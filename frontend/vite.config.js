import { defineConfig } from "vite";

export default defineConfig({
  base: "/assets/",
  build: {
    outDir: "../src/pc/web",
    emptyOutDir: true,
    rollupOptions: { output: { entryFileNames: "app.js", assetFileNames: "[name][extname]" } },
  },
  test: { environment: "jsdom", clearMocks: true },
});
