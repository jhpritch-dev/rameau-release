import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The Portal's API constants are RELATIVE (/router, /kb, /broadcaster, /arbiter, /prom) so
// the browser never names a LAN IP. In production its own nginx reverse-proxies them. The
// dev server had no equivalent, so `npm run dev` would 404 on every one of them.
//
// Rather than duplicate the upstream map here -- which would drift from nginx the moment a
// service moves -- dev proxies straight AT the deployed portal nginx and lets it do the
// routing. One source of truth, and dev exercises the same paths production does.
//
// THE TARGET IS NOT A LITERAL. It is read from RAMEAU_PORTAL_URL, which is the registry's
// name for this service (shared/resolver.py, endpoint `portal`: nginx binds 80 in its
// container and deploy/compose.yaml publishes it on 13110 by default). There is deliberately
// NO FALLBACK: the registry's rule is that an unset endpoint fails naming its variable, and a
// default here would proxy somewhere wrong while looking like it worked.
//
// The check is inside the config FUNCTION and guarded on `command`, so an unset variable
// fails `vite dev` -- which is the only mode that uses a proxy -- and never fails `vite
// build`, which does not.
export default defineConfig(({ command }) => {
  const PORTAL_NGINX = process.env.RAMEAU_PORTAL_URL
  if (command === 'serve' && !PORTAL_NGINX) {
    throw new Error(
      'RAMEAU_PORTAL_URL is unset. The dev server proxies /router, /kb, /broadcaster, ' +
      '/arbiter and /prom at the portal nginx; set it to that address, e.g. ' +
      'http://127.0.0.1:13110 for the default published port in deploy/compose.yaml.',
    )
  }
  return {
    plugins: [react()],
    server: {
      proxy: Object.fromEntries(
        ['/router', '/kb', '/broadcaster', '/arbiter', '/prom'].map(p => [
          p, { target: PORTAL_NGINX, changeOrigin: true },
        ]),
      ),
    },
  }
})
