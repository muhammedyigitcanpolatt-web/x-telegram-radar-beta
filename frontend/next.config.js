const backend = new URL(process.env.RADAR_API_URL || "http://127.0.0.1:8005");
if (!["http:", "https:"].includes(backend.protocol) || backend.username || backend.password || backend.search || backend.hash || backend.pathname !== "/") {
  throw new Error("RADAR_API_URL must be an HTTP(S) origin without credentials or a path");
}
/** @type {import('next').NextConfig} */
module.exports = {
  reactStrictMode: true,
  async rewrites() {
    return [
      { source: "/api/v1/:path*", destination: `${backend.origin}/api/v1/:path*` },
      // Next proxies Upgrade requests through external HTTP rewrites.
      { source: "/ws/:path*", destination: `${backend.origin}/ws/:path*` },
    ];
  },
};
