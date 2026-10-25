import type { Dispatch } from "preact/hooks";
import type { Api } from "../api";
import { attachmentBadges, errorText, formatDate, personName, shownAttachments } from "../format";
import type { Action } from "../state";
import type { AttachmentInfo, MessageCard as Message, Person } from "../types";

type Props = {
  message: Message;
  projectId: string;
  api: Api;
  dispatch: Dispatch<Action>;
  pinned: boolean;
};

/** One email: its pin checkbox, subject (opens the viewer), people, snippet, attachments, Open in Outlook and Reveal. */
export function MessageCard({ message, projectId, api, dispatch, pinned }: Props) {
  const fail = (e: unknown) => dispatch({ type: "setError", error: errorText(e) });
  const open = () => api.openInOutlook(projectId, message.message_key).catch(fail);
  const reveal = () => api.reveal(projectId, message.message_key).catch(fail);

  return (
    <article class="card message-card">
      <div class="card-head">
        <input
          type="checkbox"
          checked={pinned}
          aria-label={`Pin email ${message.subject}`}
          onChange={() => dispatch({ type: "togglePinKey", key: message.message_key })}
        />
        <button type="button" class="card-title" onClick={() => dispatch({ type: "select", key: message.message_key })}>
          {message.subject || "(no subject)"}
        </button>
      </div>
      <div class="meta">{formatDate(message.sent_ts)}</div>
      <People from={[message.sender]} to={message.to} cc={message.cc} />
      {message.snippet && <p class="snippet">{message.snippet}</p>}
      <Attachments attachments={message.attachments} />
      <div class="actions">
        <button type="button" class="btn primary" onClick={open}>
          Open in Outlook
        </button>
        <button type="button" class="btn" onClick={reveal}>
          Reveal in Explorer
        </button>
      </div>
    </article>
  );
}

/** The From, To and CC names of a card; each name shows its address on hover. */
export function People({ from, to, cc }: { from: Person[]; to: Person[]; cc: Person[] }) {
  const group = (label: string, people: Person[]) =>
    people.length > 0 && (
      <span class="people-group">
        <b>{label}</b>{" "}
        {people.map((person, i) => (
          <span key={`${person.addr}-${i}`}>
            {i > 0 && ", "}
            <span class="person" title={person.addr}>
              {personName(person)}
            </span>
          </span>
        ))}
      </span>
    );
  return (
    <div class="people">
      {group("From", from)}
      {group("To", to)}
      {group("CC", cc)}
    </div>
  );
}

/** A card's attachment names with their text-recovery badges; skipped inline images are not shown. */
export function Attachments({ attachments }: { attachments: AttachmentInfo[] }) {
  const shown = shownAttachments(attachments);
  if (shown.length === 0) return null;
  return (
    <ul class="attachments">
      {shown.map((attachment) => (
        <li key={attachment.ordinal}>
          <span class="attachment-name">📎 {attachment.filename || `attachment ${attachment.ordinal}`}</span>
          {attachmentBadges(attachment).map((badge) => (
            <span class="badge" key={badge}>
              {badge}
            </span>
          ))}
        </li>
      ))}
    </ul>
  );
}
