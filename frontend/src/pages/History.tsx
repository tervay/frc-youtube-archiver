import { useCallback, useEffect, useState } from "react";
import { api, Video } from "../lib/api";
import { useToast } from "../components/Toast";
import { humanBytes, timeAgo } from "../lib/format";

const STATUSES = ["", "completed", "downloading", "queued", "failed", "skipped_live", "discovered"];

export default function History() {
  const toast = useToast();
  const [items, setItems] = useState<Video[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [transcoded, setTranscoded] = useState("");
  const pageSize = 50;

  const load = useCallback(async () => {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (q) params.set("q", q);
    if (status) params.set("status", status);
    if (transcoded) params.set("transcoded", transcoded);
    try {
      const res = await api.videos(params.toString());
      setItems(res.items);
      setTotal(res.total);
    } catch (e: any) { toast(e.message, true); }
  }, [page, q, status, transcoded, toast]);

  useEffect(() => { load(); }, [load]);

  const redownload = async (v: Video) => {
    try { await api.redownload(v.id); toast(`Re-queued ${v.title}`); load(); }
    catch (e: any) { toast(e.message, true); }
  };

  const pages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <>
      <div className="page-head"><h1>History</h1><span className="muted">{total} videos</span></div>

      <div className="toolbar">
        <input placeholder="Search title / event / team / id…" value={q}
          onChange={(e) => { setPage(1); setQ(e.target.value); }} style={{ minWidth: 260 }} />
        <select value={status} onChange={(e) => { setPage(1); setStatus(e.target.value); }}>
          {STATUSES.map((s) => <option key={s} value={s}>{s || "All statuses"}</option>)}
        </select>
        <select value={transcoded} onChange={(e) => { setPage(1); setTranscoded(e.target.value); }}>
          <option value="">Any transcode</option>
          <option value="true">Transcoded (AV1)</option>
          <option value="false">Not transcoded</option>
        </select>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr><th className="wrap">Title</th><th>Status</th><th>Event</th><th>Year</th>
              <th>Codec</th><th className="right">Size</th><th>File</th><th>When</th><th></th></tr>
          </thead>
          <tbody>
            {items.map((v) => (
              <tr key={v.id}>
                <td className="wrap">
                  <a href={v.webpage_url} target="_blank" rel="noreferrer">{v.title}</a>
                  <div className="mono muted">{v.youtube_id}</div>
                </td>
                <td><span className={`badge ${v.status}`}>{v.status}</span></td>
                <td className="mono">{v.event_key || "—"}</td>
                <td>{v.year || "—"}</td>
                <td>
                  {v.current_vcodec || "—"}
                  {v.transcoded && <> <span className="badge transcoded">tdarr</span></>}
                </td>
                <td className="right mono">{humanBytes(v.current_size)}</td>
                <td>{v.file_path
                  ? (v.present ? <span className="mono muted">on disk</span>
                               : <span className="badge missing">missing</span>)
                  : <span className="muted">—</span>}</td>
                <td className="muted">{timeAgo(v.downloaded_at)}</td>
                <td className="right">
                  <button className="sm ghost" onClick={() => redownload(v)}>Re-download</button>
                </td>
              </tr>
            ))}
            {items.length === 0 && <tr><td colSpan={9}><div className="empty">No videos match.</div></td></tr>}
          </tbody>
        </table>
      </div>

      <div className="row" style={{ justifyContent: "flex-end", marginTop: 16, gap: 12 }}>
        <button className="sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>Prev</button>
        <span className="muted">Page {page} / {pages}</span>
        <button className="sm" disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</button>
      </div>
    </>
  );
}
