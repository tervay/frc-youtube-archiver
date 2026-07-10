// Tiny typed fetch wrapper around the backend API.

export type VideoStatus =
  | "discovered" | "queued" | "downloading" | "completed" | "failed"
  | "skipped_live";

export interface Video {
  id: number;
  youtube_id: string;
  title: string;
  webpage_url: string;
  source_type: string;
  event_key: string | null;
  match_key: string | null;
  year: number | null;
  team_keys: string;
  status: VideoStatus;
  file_path: string | null;
  orig_vcodec: string | null;
  current_ext: string | null;
  current_vcodec: string | null;
  current_size: number | null;
  transcoded: boolean;
  present: boolean;
  duration: number | null;
  downloaded_at: string | null;
  error: string | null;
  retry_count: number;
}

export interface Job {
  id: number;
  video_id: number;
  state: "pending" | "running" | "done" | "error" | "canceled";
  progress_pct: number;
  speed: string | null;
  eta: string | null;
  downloaded_bytes: number;
  total_bytes: number;
  attempts: number;
  log_tail: string;
}

export interface Source {
  id: number;
  kind: "season" | "district" | "team";
  value: string;
  enabled: boolean;
  notes: string;
}

async function req<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error((body as any).detail || `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  stats: () => req<any>("/stats"),
  videos: (qs: string) => req<{ total: number; items: Video[]; page: number; page_size: number }>(`/videos?${qs}`),
  redownload: (id: number) => req(`/videos/${id}/redownload`, { method: "POST" }),
  queue: (activeOnly = false) =>
    req<{ job: Job; video: Video }[]>(`/queue?active_only=${activeOnly}`),
  retry: (id: number) => req(`/queue/${id}/retry`, { method: "POST" }),
  retryFailed: () => req<{ requeued: number; total_failed: number }>(`/queue/retry-failed`, { method: "POST" }),
  cancel: (id: number) => req(`/queue/${id}/cancel`, { method: "POST" }),
  manual: (url: string, year?: number, event_key?: string) =>
    req(`/queue/manual`, { method: "POST", body: JSON.stringify({ url, year, event_key }) }),
  sources: () => req<Source[]>("/sources"),
  addSource: (kind: string, value: string, notes = "") =>
    req(`/sources`, { method: "POST", body: JSON.stringify({ kind, value, notes }) }),
  toggleSource: (id: number, enabled: boolean) =>
    req(`/sources/${id}?enabled=${enabled}`, { method: "PATCH" }),
  deleteSource: (id: number) => req(`/sources/${id}`, { method: "DELETE" }),
  settings: () => req<{ schema: any[] }>("/settings"),
  saveSettings: (values: Record<string, any>) =>
    req(`/settings`, { method: "PUT", body: JSON.stringify({ values }) }),
  scanNow: () => req(`/actions/scan`, { method: "POST" }),
  reconcileNow: () => req(`/actions/reconcile`, { method: "POST" }),
  runs: () => req<any[]>("/runs"),
};
