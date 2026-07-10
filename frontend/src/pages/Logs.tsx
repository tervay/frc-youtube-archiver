import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { useEventStream } from "../lib/events";
import { useToast } from "../components/Toast";
import { timeAgo } from "../lib/format";

export default function Logs() {
  const toast = useToast();
  const [runs, setRuns] = useState<any[]>([]);

  const load = useCallback(async () => {
    try { setRuns(await api.runs()); } catch (e: any) { toast(e.message, true); }
  }, [toast]);
  useEffect(() => { load(); }, [load]);
  useEventStream((e) => { if (e.type === "scan_done") load(); });

  return (
    <>
      <div className="page-head"><h1>Logs</h1><button className="sm" onClick={load}>Refresh</button></div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Kind</th><th>Started</th><th>Result</th><th>Discovered</th>
              <th>Enqueued / Transcoded</th><th>Errors / Missing</th><th className="wrap">Message</th></tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td><span className="badge discovered">{r.kind}</span></td>
                <td className="muted">{timeAgo(r.started_at)}</td>
                <td><span className={`badge ${r.ok ? "completed" : "failed"}`}>{r.ok ? "ok" : "error"}</span></td>
                <td>{r.discovered}</td>
                <td>{r.enqueued}</td>
                <td>{r.errors}</td>
                <td className="wrap mono muted">{r.message || "—"}</td>
              </tr>
            ))}
            {runs.length === 0 && <tr><td colSpan={7}><div className="empty">No scans have run yet.</div></td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
