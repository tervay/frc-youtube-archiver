import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { useToast } from "../components/Toast";

interface Field {
  key: string; label: string; help: string; type: string; group: string;
  value: any; is_set: boolean | null;
}

export default function Settings() {
  const toast = useToast();
  const [fields, setFields] = useState<Field[]>([]);
  const [values, setValues] = useState<Record<string, any>>({});
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const res = await api.settings();
      setFields(res.schema);
      const v: Record<string, any> = {};
      res.schema.forEach((f: Field) => { v[f.key] = f.value; });
      setValues(v);
    } catch (e: any) { toast(e.message, true); }
  }, [toast]);
  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setSaving(true);
    try { await api.saveSettings(values); toast("Settings saved"); await load(); }
    catch (e: any) { toast(e.message, true); }
    finally { setSaving(false); }
  };

  const groups = [...new Set(fields.map((f) => f.group))];
  const set = (k: string, val: any) => setValues((v) => ({ ...v, [k]: val }));

  return (
    <>
      <div className="page-head">
        <h1>Settings</h1>
        <button className="primary" onClick={save} disabled={saving}>{saving ? "Saving…" : "Save changes"}</button>
      </div>

      <div className="card" style={{ maxWidth: 720 }}>
        {groups.map((g) => (
          <div key={g} className="settings-group">
            <h3>{g}</h3>
            {fields.filter((f) => f.group === g).map((f) => (
              <div className="field" key={f.key}>
                <label>{f.label}</label>
                {f.type === "bool" ? (
                  <select value={String(values[f.key])} onChange={(e) => set(f.key, e.target.value === "true")}>
                    <option value="true">Enabled</option>
                    <option value="false">Disabled</option>
                  </select>
                ) : (
                  <input
                    type={f.type === "password" ? "password" : f.type === "int" ? "number" : "text"}
                    value={values[f.key] ?? ""}
                    placeholder={f.type === "password" && f.is_set ? "•••••• (stored — leave blank to keep)" : ""}
                    onChange={(e) => set(f.key, f.type === "int" ? Number(e.target.value) : e.target.value)}
                  />
                )}
                {f.help && <div className="help">{f.help}</div>}
              </div>
            ))}
          </div>
        ))}
      </div>
    </>
  );
}
