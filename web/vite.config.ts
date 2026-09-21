import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const target =
    loadEnv(mode, ".", "WTB_").WTB_API_URL || "http://127.0.0.1:8000";
  const proxy = { "/api": { target }, "/ws": { target, ws: true } };
  return { plugins: [react()], server: { proxy }, preview: { proxy } };
});
