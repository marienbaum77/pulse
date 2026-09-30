import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Та же политика, что в nginx (deploy/nginx.conf): проверяем интерфейс под ней ещё до сборки образа.
const CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: process.env.API_URL ?? "http://127.0.0.1:8000", changeOrigin: false },
    },
  },
  preview: { headers: { "Content-Security-Policy": CSP } },
  build: { sourcemap: false, chunkSizeWarningLimit: 700 },
});
