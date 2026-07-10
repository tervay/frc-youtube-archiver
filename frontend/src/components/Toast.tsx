import { createContext, useCallback, useContext, useState } from "react";

type Toast = { msg: string; err?: boolean } | null;
const Ctx = createContext<(msg: string, err?: boolean) => void>(() => {});

export function useToast() {
  return useContext(Ctx);
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toast, setToast] = useState<Toast>(null);
  const show = (msg: string, err = false) => {
    setToast({ msg, err });
    setTimeout(() => setToast(null), 3500);
  };
  return (
    <Ctx.Provider value={show}>
      {children}
      {toast && <div className={`toast ${toast.err ? "err" : ""}`}>{toast.msg}</div>}
    </Ctx.Provider>
  );
}
