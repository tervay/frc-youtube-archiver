import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, proxy API + SSE to the FastAPI backend on :8000.
// In prod, the build is served by FastAPI itself so no proxy is needed.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist" },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        ws: true,
      },
    },
  },
});
