import type { Dispatch } from "preact/hooks";
import type { Action, Tab } from "../state";

const TABS: [Tab, string][] = [
  ["results", "Results"],
  ["chat", "Chat"],
  ["graph", "Graph"],
  ["artefacts", "Artefacts"],
];

/** The main area's tab bar: Results | Chat | Graph | Artefacts. */
export function Tabs({ tab, dispatch }: { tab: Tab; dispatch: Dispatch<Action> }) {
  return (
    <nav class="tabs" role="tablist" aria-label="Views">
      {TABS.map(([id, label]) => (
        <button
          type="button"
          key={id}
          role="tab"
          aria-selected={tab === id ? "true" : "false"}
          class={tab === id ? "tab on" : "tab"}
          onClick={() => dispatch({ type: "setTab", tab: id })}
        >
          {label}
        </button>
      ))}
    </nav>
  );
}
