import { useState } from "preact/hooks";
import type { Dispatch } from "preact/hooks";
import { dateInputToTs, tsToDateInput } from "../format";
import type { Action } from "../state";
import type { SearchFilters } from "../types";

/** The attachment-kind chips and the server kind each one filters on. */
export const KIND_CHIPS: [label: string, kind: string][] = [
  ["PDF", "pdf"],
  ["Excel", "xlsx"],
  ["Word", "docx"],
  ["PowerPoint", "pptx"],
  ["Images", "image"],
];

type Props = { filters: SearchFilters; dispatch: Dispatch<Action> };

/** The left pane's filters: From and To/CC chips, the date range, attachment kinds and the two switches. */
export function Filters({ filters, dispatch }: Props) {
  const set = (changes: Partial<SearchFilters>) => dispatch({ type: "setFilters", filters: { ...filters, ...changes } });

  const toggleKind = (kind: string) => {
    const kinds = filters.attachment_kinds;
    set({ attachment_kinds: kinds.includes(kind) ? kinds.filter((k) => k !== kind) : [...kinds, kind] });
  };

  return (
    <div class="filters">
      <h3 class="pane-heading">From</h3>
      <AddressChips label="From" addrs={filters.from_addrs} onChange={(from_addrs) => set({ from_addrs })} />

      <h3 class="pane-heading">To / CC</h3>
      <AddressChips
        label="To/CC"
        addrs={filters.recipient_addrs}
        onChange={(recipient_addrs) => set({ recipient_addrs })}
      />

      <h3 class="pane-heading">Date</h3>
      <div class="date-row">
        <input
          type="date"
          class="input"
          aria-label="From date"
          value={tsToDateInput(filters.date_from)}
          onChange={(e) => set({ date_from: dateInputToTs(e.currentTarget.value, false) })}
        />
        <input
          type="date"
          class="input"
          aria-label="To date"
          value={tsToDateInput(filters.date_to)}
          onChange={(e) => set({ date_to: dateInputToTs(e.currentTarget.value, true) })}
        />
      </div>

      <h3 class="pane-heading">Attachments</h3>
      <div class="chips">
        {KIND_CHIPS.map(([label, kind]) => {
          const on = filters.attachment_kinds.includes(kind);
          return (
            <button
              type="button"
              key={kind}
              class={on ? "chip on" : "chip"}
              aria-pressed={on ? "true" : "false"}
              onClick={() => toggleKind(kind)}
            >
              {label}
            </button>
          );
        })}
      </div>

      <h3 class="pane-heading">Show</h3>
      <label class="switch-row">
        <span>Group by chain</span>
        <input
          type="checkbox"
          class="switch"
          checked={filters.group_by_chain}
          onChange={(e) => set({ group_by_chain: e.currentTarget.checked })}
        />
      </label>
      <label class="switch-row disabled" title="Needs the chat milestone">
        <span>Only chains with a conclusion</span>
        <input type="checkbox" class="switch" checked={false} disabled title="Needs the chat milestone" />
      </label>
    </div>
  );
}

type ChipsProps = { label: string; addrs: string[]; onChange: (addrs: string[]) => void };

/** Address chips with a remove button each and a free-text field that adds a lower-cased address on Enter. */
function AddressChips({ label, addrs, onChange }: ChipsProps) {
  const [draft, setDraft] = useState("");

  const add = () => {
    const addr = draft.trim().toLowerCase();
    if (!addr) return;
    if (!addrs.includes(addr)) onChange([...addrs, addr]);
    setDraft("");
  };

  return (
    <div class="chips">
      {addrs.map((addr) => (
        <span class="chip on" key={addr}>
          {addr}
          <button
            type="button"
            class="chip-remove"
            aria-label={`Remove ${addr}`}
            onClick={() => onChange(addrs.filter((a) => a !== addr))}
          >
            ×
          </button>
        </span>
      ))}
      <input
        class="chip-input"
        type="text"
        placeholder="+ add"
        aria-label={`Add a ${label} address`}
        value={draft}
        onInput={(e) => setDraft(e.currentTarget.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            add();
          }
        }}
      />
    </div>
  );
}
