// The JSON the Python host serialises, key for key (snake_case). No logic lives here.
// Sources: search/results.py, types.py (SearchFilters), jobs/supervisor.py (SyncStatus), catalog.py (Project) and the
// route modules under host/. Timestamps are integer seconds since the Unix epoch (UTC).

export type Person = { name: string; addr: string };

export type AttachmentInfo = {
  ordinal: number;
  filename: string;
  kind: string; // pdf, docx, xlsx, pptx, image, msg, eml or other
  size: number | null;
  extract_status: string | null;
  ocr_status: string | null;
  embedded_partial: boolean;
  page_count: number | null;
};

export type MessageCard = {
  message_key: string;
  subject: string;
  sent_ts: number | null;
  sender: Person;
  to: Person[];
  cc: Person[];
  snippet: string;
  attachments: AttachmentInfo[];
  paths: string[];
  chain_id: string | null;
  score: number;
};

export type ChainCard = {
  chain_id: string;
  subject: string;
  first_ts: number | null;
  last_ts: number | null;
  message_count: number; // the whole chain; `messages` holds only the matched ones, oldest first
  participants: { from: Person[]; to: Person[]; cc: Person[] };
  conclusion: string | null; // arrives with chain summaries (M2)
  attachments: AttachmentInfo[];
  related: [chainId: string, subject: string][];
  messages: MessageCard[];
  score: number;
};

export type SearchResponse = {
  total_messages: number;
  total_chains: number;
  page: number;
  page_size: number;
  chains: ChainCard[]; // filled when grouped by chain
  messages: MessageCard[]; // filled when not
  mode: "hybrid" | "keyword_only";
};

/** The search filters; the comment on each field is the Python default (an absent field takes it on the host). */
export type SearchFilters = {
  query: string; // ""
  from_addrs: string[]; // []
  recipient_addrs: string[]; // []
  date_from: number | null; // null
  date_to: number | null; // null
  attachment_kinds: string[]; // []
  has_attachments: boolean | null; // null
  group_by_chain: boolean; // true
  only_with_conclusion: boolean; // false
  sort: "relevance" | "date"; // "relevance"
};

export type SyncStatus = {
  project_id: string;
  state: "idle" | "scanning" | "parsing" | "ocr" | "reindexing";
  last_scan_at: number | null;
  files_total: number;
  files_pending: number;
  ocr_pending: number;
  metadata_only: number;
  last_error: string | null;
};

export type Project = { id: string; name: string; created_at: number; dir: string };

/** The body of `GET /api/projects/{id}`: the project plus its watched folders (empty when it is not open). */
export type ProjectDetail = Project & { folders: string[] };

/** The body of `GET /api/status`; `blocked` is the reason a project has no running supervisor, else null. */
export type Status = {
  version: string;
  degraded: string[]; // "embedder" and/or "ocr" when that part is unavailable
  projects: { id: string; name: string; created_at: number; sync: SyncStatus | null; blocked: string | null }[];
};

/** The body of `GET /api/settings`. */
export type Settings = {
  data_root: string;
  port: number;
  log_level: "DEBUG" | "INFO" | "WARNING" | "ERROR";
  autostart: { enabled: boolean; available: boolean };
  version: string;
};

/** The body of `GET /api/projects/{id}/settings`. */
export type ProjectSettings = {
  reconcile_interval_s: number;
  ocr_enabled: boolean;
  ocr_engine_order: ("windows" | "rapid")[];
  export_dir: string | null;
  group_by_chain_default: boolean;
};

/** The body of `POST /api/projects/{id}/export/excel`'s answer. */
export type ExportInfo = { path: string; name: string; rows: number; truncated: boolean };

export type SavedSearch = { name: string; filters: SearchFilters };

/** The body of `GET /api/projects/{id}/messages/{key}`. */
export type MessageDetail = {
  message: {
    headers: {
      subject: string;
      message_id: string | null;
      sent_ts: number | null;
      received_ts: number | null;
      date_source: string;
    };
    sender: Person;
    to: Person[];
    cc: Person[];
    sent_ts: number | null;
    body_html_sanitized: string | null; // already sanitised by the host
    body_text: string | null;
    new_text: string | null;
  };
  attachments: AttachmentInfo[];
  paths: string[];
  chain: { chain_id: string | null; messages: MessageCard[] };
};
