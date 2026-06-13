import { defineConfig } from 'vite'

// The simulator HTTP service runs on :8200. Default to it but allow override
// with VITE_SIM_API when the service is on another host/port.
export default defineConfig({
  server: {
    host: '127.0.0.1',
    port: 5174,
  },
})
