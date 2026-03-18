import adapter from "@sveltejs/adapter-cloudflare";
import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

/** @type {import('@sveltejs/kit').Config} */
const config = {
  preprocess: vitePreprocess(),
  kit: {
    csrf: { trustedOrigins: ["*"] },
    adapter: adapter({
      config: "wrangler.toml",
      platformProxy: {
        configPath: "wrangler.toml",
        persist: true,
      },
    }),
  },
};

export default config;
