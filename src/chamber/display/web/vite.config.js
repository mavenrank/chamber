import { defineConfig } from "vite";

export default defineConfig({
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    assetsDir: "assets",
    rollupOptions: {
      output: {
        entryFileNames: "desk.js",
        chunkFileNames: "desk.js",
        assetFileNames: "desk.[ext]",
      },
    },
  },
});
