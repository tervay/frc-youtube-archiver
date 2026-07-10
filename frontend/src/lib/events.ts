import { useEffect, useRef, useState } from "react";

export interface ArchiverEvent {
  type: string;
  data: any;
}

// Subscribe to the backend SSE stream. `onEvent` fires for every event.
export function useEventStream(onEvent: (e: ArchiverEvent) => void) {
  const cb = useRef(onEvent);
  cb.current = onEvent;
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const es = new EventSource("/api/events");
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.onmessage = (m) => {
      try {
        cb.current(JSON.parse(m.data));
      } catch {
        /* ignore keep-alives */
      }
    };
    return () => es.close();
  }, []);

  return connected;
}
