import { NavLink, Route, Routes } from "react-router-dom";
import { useEventStream } from "./lib/events";
import { ToastProvider } from "./components/Toast";
import Dashboard from "./pages/Dashboard";
import History from "./pages/History";
import Queue from "./pages/Queue";
import Sources from "./pages/Sources";
import Settings from "./pages/Settings";
import Logs from "./pages/Logs";

const NAV = [
  { to: "/", label: "Dashboard", icon: "📊", end: true },
  { to: "/history", label: "History", icon: "🗂️" },
  { to: "/queue", label: "Queue", icon: "⬇️" },
  { to: "/sources", label: "Sources", icon: "🎯" },
  { to: "/settings", label: "Settings", icon: "⚙️" },
  { to: "/logs", label: "Logs", icon: "📜" },
];

export default function App() {
  const connected = useEventStream(() => {});

  return (
    <ToastProvider>
      <div className="app">
        <aside className="sidebar">
          <div className="brand">FRC <span>Archiver</span></div>
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.end}
              className={({ isActive }) => "nav-link" + (isActive ? " active" : "")}>
              <span>{n.icon}</span> {n.label}
            </NavLink>
          ))}
          <div className="sidebar-foot">
            <span className={`dot ${connected ? "on" : "off"}`} />
            {connected ? "Live" : "Reconnecting…"}
          </div>
        </aside>
        <main className="main">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/history" element={<History />} />
            <Route path="/queue" element={<Queue />} />
            <Route path="/sources" element={<Sources />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/logs" element={<Logs />} />
          </Routes>
        </main>
      </div>
    </ToastProvider>
  );
}
