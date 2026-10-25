import preact from "@preact/preset-vite";
import { defineConfig } from "vitest/config";

// The UI is always served by the Python host, so there is no dev server or proxy: build into the package's `ui/dist`.
export default defineConfig({
  plugins: [preact()],
  base: "/",
  build: { outDir: "../src/emailsupersearch/ui/dist", emptyOutDir: true },
  test: { environment: "jsdom" },
});
