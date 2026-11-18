import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Port 1420 is fixed: tauri.conf.json's devUrl and the engine's allowed origins name it.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: { port: 1420, strictPort: true },
  preview: { port: 1420, strictPort: true },
});
