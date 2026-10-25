import { useState } from "preact/hooks";
import type { Dispatch } from "preact/hooks";
import type { Api } from "../api";
import { errorText, formatSpan } from "../format";
import type { Action } from "../state";
import type { ChainCard as Chain, MessageCard as Message } from "../types";
import { Attachments, MessageCard, People } from "./MessageCard";

type Props = {
  chain: Chain;
  projectId: string;
  api: Api;
  dispatch: Dispatch<Action>;
  pinnedChainIds: Set<string>;
  pinnedKeys: Set<string>;
};

/** The matched message a chain card stands for: the best score, then the newest. */
export function leadMessage(chain: Chain): Message | null {
  let lead: Message | null = null;
  for (const message of chain.messages) {
    if (
      lead === null ||
      message.score > lead.score ||
      (message.score === lead.score && (message.sent_ts ?? -Infinity) > (lead.sent_ts ?? -Infinity))
    ) {
      lead = message;
    }
  }
  return lead;
}

/** One email chain: pin checkbox, subject, span, people, conclusion, attachments, related chains and actions. */
export function ChainCard({ chain, projectId, api, dispatch, pinnedChainIds, pinnedKeys }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [allMessages, setAllMessages] = useState<Message[] | null>(null);
  const lead = leadMessage(chain);
  const count = chain.message_count;

  const fail = (e: unknown) => dispatch({ type: "setError", error: errorText(e) });

  const toggleAll = () => {
    const next = !expanded;
    setExpanded(next);
    // The card holds only the matched messages; the viewer's chain list has every live one.
    if (next && allMessages === null && lead !== null && chain.messages.length < count) {
      api
        .message(projectId, lead.message_key)
        .then((detail) => setAllMessages(detail.chain.messages))
        .catch(fail);
    }
  };

  const shown = allMessages ?? chain.messages;

  return (
    <article class="card chain-card">
      <div class="card-head">
        <input
          type="checkbox"
          checked={pinnedChainIds.has(chain.chain_id)}
          aria-label={`Pin chain ${chain.subject}`}
          onChange={() => dispatch({ type: "togglePinChain", chainId: chain.chain_id })}
        />
        <button
          type="button"
          class="card-title"
          disabled={lead === null}
          onClick={() => lead && dispatch({ type: "select", key: lead.message_key })}
        >
          {chain.subject || "(no subject)"}
        </button>
      </div>
      <div class="meta">
        {count === 1 ? "1 email" : `${count} emails`} · {formatSpan(chain.first_ts, chain.last_ts)}
      </div>
      <People from={chain.participants.from} to={chain.participants.to} cc={chain.participants.cc} />
      {chain.conclusion && (
        <div class="conclusion">
          <div class="conclusion-tag">Conclusion of the chain</div>
          {chain.conclusion}
        </div>
      )}
      {lead?.snippet && <p class="snippet">{lead.snippet}</p>}
      <Attachments attachments={chain.attachments} />
      {chain.related.length > 0 && (
        <div class="related">
          ↔ Related chains:{" "}
          {chain.related.map(([id, subject]) => (
            <span class="related-chain" key={id}>
              {subject || "(no subject)"}
            </span>
          ))}
        </div>
      )}
      <div class="actions">
        <button
          type="button"
          class="btn primary"
          disabled={lead === null}
          onClick={() => lead && api.openInOutlook(projectId, lead.message_key).catch(fail)}
        >
          Open in Outlook
        </button>
        <button
          type="button"
          class="btn"
          disabled={lead === null}
          onClick={() => lead && api.reveal(projectId, lead.message_key).catch(fail)}
        >
          Reveal in Explorer
        </button>
        <button type="button" class="btn" disabled title="Needs the chat milestone">
          Add to chat
        </button>
        <button type="button" class="btn" disabled title="Needs the graph milestone">
          Show in graph
        </button>
        {count > 1 && (
          <button type="button" class="btn" aria-expanded={expanded ? "true" : "false"} onClick={toggleAll}>
            All {count} emails
          </button>
        )}
      </div>
      {expanded && (
        <div class="chain-messages">
          {shown.map((message) => (
            <MessageCard
              key={message.message_key}
              message={message}
              projectId={projectId}
              api={api}
              dispatch={dispatch}
              pinned={pinnedKeys.has(message.message_key)}
            />
          ))}
        </div>
      )}
    </article>
  );
}
