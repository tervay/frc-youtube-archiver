import { useCallback, useEffect, useState } from "react";
import { api, Job, Video } from "../lib/api";
import { useEventStream } from "../lib/events";
import { useToast } from "../components/Toast";
import { humanBytes } from "../lib/format";

interface Row { job: Job; video: Video; }

export default function Queue() {
  const toast = useToast();
  const [rows, setRows] = useState<Row[]>([]);
  const [url, setUrl] = useState("");

  const load = useCallback(async () => {
    try { setRows(await api.queue(false)); }
    catch (e: any) { toast(e.message, true); }
  }, [toast]);

  useEffect(() => { load(); }, [load]);
  useEventStream((e) => {
    if (e.type === "progress") {
      setRows((rs) => rs.map((r) => r.job.id === e.data.job_id
        ? { ...r, job: { ...r.job, progress_pct: e.data.progress_pct } } : r));
    } else { load(); }
  });

  const addManual = async () => {
    if (!url.trim()) return;
    try { await api.manual(url.trim()); toast("Queued"); setUrl(""); load(); }
    catch (e: any) { toast(e.message, true); }
  };
  const act = async (fn: Promise<any>, ok: string) => {
    try { await fn; toast(ok); load(); } catch (e: any) { toast(e.message, true); }
  };

  const active = rows.filter((r) => ["pending", "running"].includes(r.job.state));
  const finished = rows.filter((r) => !["pending", "running"].includes(r.job.state));

  const retryAllFailed = async () => {
    if (!confirm("Requeue all failed videos?")) return;
    try {
      const r = await api.retryFailed();
      toast(`Requeued ${r.requeued} of ${r.total_failed} failed`);
      load();
    } catch (e: any) { toast(e.message, true); }
  };

  return (
    <>
      <div className="page-head">
        <h1>Queue</h1>
        <button onClick={retryAllFailed}>Retry all failed</button>
      </div>

      <div className="card" style={{ marginBottom: 20 }}>
        <div className="row">
          <input placeholder="Paste a YouTube URL or 11-char video ID…" value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && addManual()} />
          <button className="primary" onClick={addManual}>Add</button>
        </div>
        <div className="help muted" style={{ marginTop: 8 }}>Manually queue a one-off download outside of TBA.</div>
      </div>

      <h3 style={{ margin: "8px 0 12px" }}>Active & pending ({active.length})</h3>
      <QueueTable rows={active} act={act} empty="Nothing queued." showProgress />

      <h3 style={{ margin: "28px 0 12px" }}>Recent & failed</h3>
      <QueueTable rows={finished} act={act} empty="No history yet." />
    </>
  );
}

function QueueTable({ rows, act, empty, showProgress }:
  { rows: Row[]; act: (p: Promise<any>, ok: string) => void; empty: string; showProgress?: boolean }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr><th className="wrap">Title</th><th>State</th>
            {showProgress && <th>Progress</th>}<th>Attempts</th><th className="wrap">Last error</th><th></th></tr>
        </thead>
        <tbody>
          {rows.map(({ job, video }) => (
            <tr key={job.id}>
              <td className="wrap">{video.title}<div className="mono muted">{video.event_key || video.youtube_id}</div></td>
              <td><span className={`badge ${job.state}`}>{job.state}</span></td>
              {showProgress && <td>
                <div className="progress"><div style={{ width: `${job.progress_pct}%` }} /></div>
              </td>}
              <td>{job.attempts}</td>
              <td className="wrap mono muted">{job.log_tail ? job.log_tail.split("\n").slice(-1)[0] : "—"}</td>
              <td className="right">
                {job.state === "pending" && <button className="sm ghost danger" onClick={() => act(api.cancel(job.id), "Canceled")}>Cancel</button>}
                {["error", "canceled", "done"].includes(job.state) && <button className="sm ghost" onClick={() => act(api.retry(job.id), "Retrying")}>Retry</button>}
              </td>
            </tr>
          ))}
          {rows.length === 0 && <tr><td colSpan={showProgress ? 6 : 5}><div className="empty">{empty}</div></td></tr>}
        </tbody>
      </table>
    </div>
  );
}
