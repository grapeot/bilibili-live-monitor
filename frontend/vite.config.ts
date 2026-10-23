import { defineConfig, loadEnv } from 'vite';
export default defineConfig(({mode}) => {
  const env = loadEnv(mode, '..', '');
  const base = (process.env.APP_ROOT_PATH ?? env.APP_ROOT_PATH ?? '').replace(/\/$/, '') + '/';
  return {base, server: {proxy: {[base + 'api']: `http://127.0.0.1:${env.APP_PORT || '8765'}`}}};
});
