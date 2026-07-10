import { useCallback, useEffect, useState } from "react";
import { api, Job, Video } from "../lib/api";
import { useEventStream } from "../lib/events";
import { useToast } from "../components/Toast";
import { humanBytes, timeAgo } from "../lib/format";

interface ActiveRow { job: Job; video: Video; }

// A post-processing phase label (e.g. "merging…") rather than a transfer speed.
const isPhase = (s: string | null) => !!s && s.endsWith("…");

export default function Dashboard() {
  const toast = useToast();
  const [stats, setStats] = useState<any>(null);
  const [active, setActive] = useState<ActiveRow[]>([]);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, q] = await Promise.all([api.stats(), api.queue(true)]);
      setStats(s);
      setActive(q);
    } catch (e: any) { toast(e.message, true); }
  }, [toast]);

  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);

  // Live progress: patch matching rows in place, reload on lifecycle events.
  useEventStream((e) => {
    if (e.type === "progress") {
      setActive((rows) => rows.map((r) =>
        r.job.id === e.data.job_id
          ? { ...r, job: { ...r.job, progress_pct: e.data.progress_pct,
                           speed: e.data.speed, eta: e.data.eta,
                           downloaded_bytes: e.data.downloaded_bytes ?? r.job.downloaded_bytes,
                           total_bytes: e.data.total_bytes ?? r.job.total_bytes } }
          : r));
    } else if (["job_done", "job_started", "job_queued", "job_error", "job_skipped", "scan_done"].includes(e.type)) {
      load();
    }
  });

  const run = async (name: string, fn: () => Promise<any>, ok: string) => {
    setBusy(name);
    try { await fn(); toast(ok); await load(); }
    catch (e: any) { toast(e.message, true); }
    finally { setBusy(null); }
  };

  const nextScan = stats?.next_run?.scan ? new Date(stats.next_run.scan).toLocaleString() : "—";

  return (
    <>
      <div className="page-head">
        <h1>Dashboard</h1>
        <div className="row">
          <button className="primary" disabled={busy !== null}
            onClick={() => run("scan", api.scanNow, "Scan started")}>
            {busy === "scan" ? "Scanning…" : "Scan now"}
          </button>
          <button disabled={busy !== null}
            onClick={() => run("rec", api.reconcileNow, "Reconcile done")}>
            {busy === "rec" ? "Reconciling…" : "Reconcile now"}
          </button>
        </div>
      </div>

      <div className="grid stats">
        <Stat label="Total videos" value={stats?.videos_total ?? "—"} />
        <Stat label="Total size" value={stats ? humanBytes(stats.total_size) : "—"} small />
        <Stat label="Completed" value={stats?.by_status?.completed ?? "—"} />
        <Stat label="Downloaded 24h" value={stats?.completed_24h ?? "—"} />
        <Stat label="Downloading" value={stats?.queue?.running ?? "—"} />
        <Stat label="Queued" value={stats?.queue?.pending ?? "—"} />
        <Stat label="Transcoded" value={stats?.transcoded ?? "—"} />
        <Stat label="Missing files" value={stats?.missing ?? "—"} />
        <Stat label="Failed" value={stats?.queue?.error ?? "—"} />
      </div>

      <div className="card">
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
          <strong>Active downloads</strong>
          <span className="muted">Next scan: {nextScan}</span>
        </div>
        {active.length === 0 ? (
          <div className="empty">No active downloads. Idle. 😴</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th className="wrap">Title</th><th>Event</th><th>Progress</th>
                  <th>Speed</th><th>ETA</th><th className="right">Size</th></tr>
              </thead>
              <tbody>
                {active.map(({ job, video }) => (
                  <tr key={job.id}>
                    <td className="wrap">{video.title}</td>
                    <td className="mono">{video.event_key || "—"}</td>
                    <td>
                      <div className="row">
                        <div className="progress"><div style={{ width: `${job.progress_pct}%` }} /></div>
                        <span className="muted" style={{ minWidth: 42 }}>{job.progress_pct.toFixed(0)}%</span>
                        {isPhase(job.speed) && <span className="badge downloading">{job.speed}</span>}
                      </div>
                    </td>
                    <td className="mono">{isPhase(job.speed) ? "—" : (job.state === "running" ? (job.speed || "…") : job.state)}</td>
                    <td className="mono">{isPhase(job.speed) ? "—" : (job.eta || "—")}</td>
                    <td className="right mono">{humanBytes(job.total_bytes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}

function Stat({ label, value, small }: { label: string; value: any; small?: boolean }) {
  return (
    <div className="card stat">
      <div className="label">{label}</div>
      <div className={"value" + (small ? " small" : "")}>{value}</div>
    </div>
  );
}
