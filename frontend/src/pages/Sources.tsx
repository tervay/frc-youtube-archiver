import { useCallback, useEffect, useState } from "react";
import { api, Source } from "../lib/api";
import { useToast } from "../components/Toast";

const FIRST_FRC_YEAR = 1992;
const CURRENT_YEAR = new Date().getFullYear();
const YEARS = Array.from({ length: CURRENT_YEAR - FIRST_FRC_YEAR + 1 }, (_, i) => CURRENT_YEAR - i);

const KIND_HELP: Record<string, string> = {
  season: "A 4-digit year, e.g. 2026 — scans every event's YouTube VODs that year.",
  district: "A district key, e.g. 2026ne — scans that district's event VODs.",
  team: "A team, e.g. 254 or frc254 — downloads that team's match videos across the year range you pick. Leave the end on “Ongoing” to keep following the team every new season.",
  event_type: "TBA event types, e.g. 3,4,5,6 — downloads every match video from events of those types, every year (3 = CMP division, 4 = CMP finals, 5 = district CMP division, 6 = Festival of Champions).",
};

function yearRange(s: Source): string {
  if (s.kind !== "team" || s.start_year === null) return "—";
  if (s.end_year === null) return `${s.start_year}–ongoing`;
  if (s.end_year === s.start_year) return `${s.start_year}`;
  return `${s.start_year}–${s.end_year}`;
}

function YearSelects({
  start, end, onStart, onEnd,
}: {
  start: number; end: number | null;
  onStart: (y: number) => void; onEnd: (y: number | null) => void;
}) {
  return (
    <>
      <select value={start} onChange={(e) => onStart(Number(e.target.value))}>
        {YEARS.map((y) => <option key={y} value={y}>{y}</option>)}
      </select>
      <span className="muted">–</span>
      <select value={end === null ? "" : end} onChange={(e) => onEnd(e.target.value === "" ? null : Number(e.target.value))}>
        <option value="">Ongoing</option>
        {YEARS.filter((y) => y >= start).map((y) => <option key={y} value={y}>{y}</option>)}
      </select>
    </>
  );
}

export default function Sources() {
  const toast = useToast();
  const [sources, setSources] = useState<Source[]>([]);
  const [kind, setKind] = useState("team");
  const [value, setValue] = useState("");
  const [start, setStart] = useState(FIRST_FRC_YEAR);
  const [end, setEnd] = useState<number | null>(null);
  const [editing, setEditing] = useState<number | null>(null);
  const [editStart, setEditStart] = useState(FIRST_FRC_YEAR);
  const [editEnd, setEditEnd] = useState<number | null>(null);

  const load = useCallback(async () => {
    try { setSources(await api.sources()); } catch (e: any) { toast(e.message, true); }
  }, [toast]);
  useEffect(() => { load(); }, [load]);

  const add = async () => {
    if (!value.trim()) return;
    const years = kind === "team" ? { start, end } : { start: undefined, end: undefined };
    try {
      await api.addSource(kind, value.trim(), "", years.start, years.end ?? undefined);
      toast("Source added"); setValue(""); load();
    } catch (e: any) { toast(e.message, true); }
  };
  const toggle = async (s: Source) => {
    try { await api.toggleSource(s.id, !s.enabled); load(); } catch (e: any) { toast(e.message, true); }
  };
  const remove = async (s: Source) => {
    if (!confirm(`Remove ${s.kind} ${s.value}?`)) return;
    try { await api.deleteSource(s.id); toast("Removed"); load(); } catch (e: any) { toast(e.message, true); }
  };
  const beginEdit = (s: Source) => {
    setEditing(s.id);
    setEditStart(s.start_year ?? FIRST_FRC_YEAR);
    setEditEnd(s.end_year);
  };
  const saveEdit = async (s: Source) => {
    try {
      await api.setSourceYears(s.id, editStart, editEnd);
      setEditing(null); toast("Years updated"); load();
    } catch (e: any) { toast(e.message, true); }
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
              <option value="event_type">Event type</option>
            </select>
          </div>
          <div style={{ flex: 1 }}>
            <label className="muted" style={{ fontSize: 12 }}>Value</label>
            <input placeholder={kind === "team" ? "254" : kind === "season" ? "2026" : kind === "event_type" ? "3,4,5,6" : "2026ne"}
              value={value} onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && add()} />
          </div>
          {kind === "team" && (
            <div>
              <label className="muted" style={{ fontSize: 12 }}>Years</label>
              <div className="row" style={{ alignItems: "center", gap: 6 }}>
                <YearSelects start={start} end={end} onStart={(y) => { setStart(y); if (end !== null && end < y) setEnd(y); }} onEnd={setEnd} />
              </div>
            </div>
          )}
          <button className="primary" onClick={add}>Add source</button>
        </div>
        <div className="help muted" style={{ marginTop: 10 }}>{KIND_HELP[kind]}</div>
      </div>

      <div className="table-wrap">
        <table>
          <thead><tr><th>Kind</th><th>Value</th><th>Years</th><th>Enabled</th><th></th></tr></thead>
          <tbody>
            {sources.map((s) => (
              <tr key={s.id}>
                <td><span className="badge discovered">{s.kind}</span></td>
                <td className="mono">{s.value}</td>
                <td>
                  {editing === s.id ? (
                    <div className="row" style={{ alignItems: "center", gap: 6 }}>
                      <YearSelects start={editStart} end={editEnd}
                        onStart={(y) => { setEditStart(y); if (editEnd !== null && editEnd < y) setEditEnd(y); }}
                        onEnd={setEditEnd} />
                      <button className="sm primary" onClick={() => saveEdit(s)}>Save</button>
                      <button className="sm ghost" onClick={() => setEditing(null)}>Cancel</button>
                    </div>
                  ) : s.kind === "team" ? (
                    <button className="sm ghost mono" onClick={() => beginEdit(s)}>{yearRange(s)}</button>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td>
                  <button className="sm ghost" onClick={() => toggle(s)}>
                    {s.enabled ? "✅ Enabled" : "⏸️ Disabled"}
                  </button>
                </td>
                <td className="right"><button className="sm ghost danger" onClick={() => remove(s)}>Delete</button></td>
              </tr>
            ))}
            {sources.length === 0 && <tr><td colSpan={5}><div className="empty">No sources yet. Add a team, season, or district to start archiving.</div></td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
