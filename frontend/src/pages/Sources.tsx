import { useCallback, useEffect, useState } from "react";
import { api, Source } from "../lib/api";
import { useToast } from "../components/Toast";

const KIND_HELP: Record<string, string> = {
  season: "A 4-digit year, e.g. 2026 — scans every event's YouTube VODs that year.",
  district: "A district key, e.g. 2026ne — scans that district's event VODs.",
  team: "A team, e.g. 254 or frc254 — downloads all of that team's match videos.",
};

export default function Sources() {
  const toast = useToast();
  const [sources, setSources] = useState<Source[]>([]);
  const [kind, setKind] = useState("team");
  const [value, setValue] = useState("");

  const load = useCallback(async () => {
    try { setSources(await api.sources()); } catch (e: any) { toast(e.message, true); }
  }, [toast]);
  useEffect(() => { load(); }, [load]);

  const add = async () => {
    if (!value.trim()) return;
    try { await api.addSource(kind, value.trim()); toast("Source added"); setValue(""); load(); }
    catch (e: any) { toast(e.message, true); }
  };
  const toggle = async (s: Source) => {
    try { await api.toggleSource(s.id, !s.enabled); load(); } catch (e: any) { toast(e.message, true); }
  };
  const remove = async (s: Source) => {
    if (!confirm(`Remove ${s.kind} ${s.value}?`)) return;
    try { await api.deleteSource(s.id); toast("Removed"); load(); } catch (e: any) { toast(e.message, true); }
  };

  return (
    <>
      <div className="page-head"><h1>Sources</h1></div>

      <div className="card" style={{ marginBottom: 20 }}>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div style={{ width: 160 }}>
            <label className="muted" style={{ fontSize: 12 }}>Kind</label>
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="team">Team</option>
              <option value="season">Season (year)</option>
              <option value="district">District</option>
            </select>
          </div>
          <div style={{ flex: 1 }}>
            <label className="muted" style={{ fontSize: 12 }}>Value</label>
            <input placeholder={kind === "team" ? "254" : kind === "season" ? "2026" : "2026ne"}
              value={value} onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && add()} />
          </div>
          <button className="primary" onClick={add}>Add source</button>
        </div>
        <div className="help muted" style={{ marginTop: 10 }}>{KIND_HELP[kind]}</div>
      </div>

      <div className="table-wrap">
        <table>
          <thead><tr><th>Kind</th><th>Value</th><th>Enabled</th><th></th></tr></thead>
          <tbody>
            {sources.map((s) => (
              <tr key={s.id}>
                <td><span className="badge discovered">{s.kind}</span></td>
                <td className="mono">{s.value}</td>
                <td>
                  <button className="sm ghost" onClick={() => toggle(s)}>
                    {s.enabled ? "✅ Enabled" : "⏸️ Disabled"}
                  </button>
                </td>
                <td className="right"><button className="sm ghost danger" onClick={() => remove(s)}>Delete</button></td>
              </tr>
            ))}
            {sources.length === 0 && <tr><td colSpan={4}><div className="empty">No sources yet. Add a team, season, or district to start archiving.</div></td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
