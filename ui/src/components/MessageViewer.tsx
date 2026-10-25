import { useEffect, useState } from "preact/hooks";
import type { Dispatch } from "preact/hooks";
import type { Api } from "../api";
import { attachmentBadges, errorText, formatDate, formatDateTime, personName, shownAttachments } from "../format";
import type { Action } from "../state";
import type { MessageDetail, Person } from "../types";

type Props = { projectId: string; messageKey: string; api: Api; dispatch: Dispatch<Action> };

/** The right-side drawer: one email's headers, sanitised body, attachments (downloadable) and its chain. */
export function MessageViewer({ projectId, messageKey, api, dispatch }: Props) {
  const [detail, setDetail] = useState<MessageDetail | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    setDetail(null);
    setFailure(null);
    let live = true;
    api
      .message(projectId, messageKey)
      .then((d) => live && setDetail(d))
      .catch((e) => live && setFailure(errorText(e)));
    return () => {
      live = false;
    };
  }, [api, projectId, messageKey]);

  const close = () => dispatch({ type: "select", key: null });
  const message = detail?.message;
  const attachments = detail ? shownAttachments(detail.attachments) : [];

  return (
    <aside class="drawer" aria-label="Email viewer">
      <div class="drawer-head">
        <h2 class="drawer-title">{message ? message.headers.subject || "(no subject)" : "Email"}</h2>
        <button type="button" class="btn" onClick={close}>
          Close
        </button>
      </div>
      {failure && <p class="error-text">{failure}</p>}
      {!detail && !failure && <p class="muted">Loading…</p>}
      {detail && message && (
        <div class="drawer-body">
          <dl class="headers">
            <dt>From</dt>
            <dd>
              <PersonList people={[message.sender]} />
            </dd>
            {message.to.length > 0 && (
              <>
                <dt>To</dt>
                <dd>
                  <PersonList people={message.to} />
                </dd>
              </>
            )}
            {message.cc.length > 0 && (
              <>
                <dt>CC</dt>
                <dd>
                  <PersonList people={message.cc} />
                </dd>
              </>
            )}
            <dt>Date</dt>
            <dd>{formatDateTime(message.sent_ts)}</dd>
          </dl>

          {message.body_html_sanitized !== null ? (
            <iframe class="message-body" title="Email body" sandbox="" srcdoc={message.body_html_sanitized} />
          ) : (
            <pre class="message-text">{message.body_text ?? ""}</pre>
          )}

          {attachments.length > 0 && (
            <section>
              <h3 class="pane-heading">Attachments</h3>
              <ul class="attachments">
                {attachments.map((attachment) => (
                  <li key={attachment.ordinal}>
                    <a href={api.attachmentUrl(projectId, messageKey, attachment.ordinal)} download>
                      📎 {attachment.filename || `attachment ${attachment.ordinal}`}
                    </a>
                    {attachmentBadges(attachment).map((badge) => (
                      <span class="badge" key={badge}>
                        {badge}
                      </span>
                    ))}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {detail.chain.messages.length > 1 && (
            <section>
              <h3 class="pane-heading">Chain</h3>
              <ul class="chain-list">
                {detail.chain.messages.map((card) => (
                  <li key={card.message_key}>
                    <button
                      type="button"
                      class={card.message_key === messageKey ? "chain-item on" : "chain-item"}
                      aria-current={card.message_key === messageKey ? "true" : undefined}
                      onClick={() => dispatch({ type: "select", key: card.message_key })}
                    >
                      <span>{personName(card.sender)}</span>
                      <span class="muted"> · {formatDate(card.sent_ts)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}
    </aside>
  );
}

function PersonList({ people }: { people: Person[] }) {
  return (
    <>
      {people.map((person, i) => (
        <span key={`${person.addr}-${i}`}>
          {i > 0 && ", "}
          {person.name.trim() ? `${person.name} <${person.addr}>` : person.addr}
        </span>
      ))}
    </>
  );
}
