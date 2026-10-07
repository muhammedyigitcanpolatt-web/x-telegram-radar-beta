// Real Next production server + loopback-only backend fixture; no external service.
import assert from "node:assert/strict";
import http from "node:http";
import { createHash, randomBytes } from "node:crypto";
import { spawn } from "node:child_process";
import { once } from "node:events";

const origin = "http://127.0.0.1:3105";
const cookie = "radar_session=fixture-session";
const upgrades = [];
const sockets = new Set();
const backend = http.createServer((request, response) => {
  const authenticated = request.headers.cookie === cookie;
  response.setHeader("Content-Type", "application/json");
  if (request.url === "/api/v1/auth/login") {
    assert.equal(request.headers.origin, origin);
    let body = "";
    request.on("data", (chunk) => { body += chunk; });
    request.on("end", () => {
      assert.deepEqual(JSON.parse(body), { username: "fixture", password: "test-only" });
      response.setHeader("Set-Cookie", `${cookie}; Path=/; HttpOnly; SameSite=Strict`);
      response.end(JSON.stringify({ username: "fixture", role: "analyst" }));
    });
  } else if (request.url === "/api/v1/auth/me") {
    response.statusCode = authenticated ? 200 : 401;
    response.end(JSON.stringify(authenticated ? { username: "fixture", role: "analyst" } : { detail: "Authentication required" }));
  } else if (request.url === "/api/v1/auth/logout") {
    assert.equal(authenticated, true);
    response.statusCode = 204;
    response.setHeader("Set-Cookie", "radar_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict");
    response.end();
  } else { response.statusCode = 404; response.end("{}"); }
});
backend.on("connection", (socket) => {
  sockets.add(socket);
  socket.on("close", () => sockets.delete(socket));
});
backend.on("upgrade", (request, socket) => {
  upgrades.push({ url: request.url, cookie: request.headers.cookie, origin: request.headers.origin });
  if (request.headers.cookie !== cookie || request.headers.origin !== origin) {
    socket.end("HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n");
    return;
  }
  const accept = createHash("sha1").update(request.headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest("base64");
  socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
});

let server;
try {
  // Fail rather than touch a real backend if this port is already occupied.
  backend.listen(8005, "127.0.0.1");
  await once(backend, "listening");
  server = spawn(process.execPath, ["node_modules/next/dist/bin/next", "start", "-H", "127.0.0.1", "-p", "3105"], { stdio: ["ignore", "pipe", "pipe"] });
  let serverLog = "";
  server.stdout.on("data", (chunk) => { serverLog += chunk; });
  server.stderr.on("data", (chunk) => { serverLog += chunk; });
  let ready = false;
  for (let attempt = 0; attempt < 120; attempt++) {
    if (server.exitCode !== null) throw new Error(serverLog);
    try { if ((await fetch(origin)).ok) { ready = true; break; } } catch { /* booting */ }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  assert.ok(ready, serverLog);
  assert.equal((await fetch(`${origin}/review`)).status, 200);
  assert.equal((await fetch(`${origin}/api/v1/auth/me`)).status, 401);
  const login = await fetch(`${origin}/api/v1/auth/login`, {
    method: "POST", headers: { "Content-Type": "application/json", Origin: origin },
    body: JSON.stringify({ username: "fixture", password: "test-only" }),
  });
  assert.equal(login.status, 200);
  assert.match(login.headers.get("set-cookie"), /HttpOnly/);
  assert.deepEqual(await login.json(), { username: "fixture", role: "analyst" });
  const me = await fetch(`${origin}/api/v1/auth/me`, { headers: { Cookie: cookie } });
  assert.equal(me.status, 200);
  assert.equal((await me.json()).role, "analyst");
  const upgrade = (authenticated) => new Promise((resolve, reject) => {
    const request = http.request(`${origin}/ws/signals`, { headers: {
      Upgrade: "websocket", Connection: "Upgrade", "Sec-WebSocket-Version": "13",
      "Sec-WebSocket-Key": randomBytes(16).toString("base64"), Origin: origin,
      ...(authenticated ? { Cookie: cookie } : {}),
    } });
    request.setTimeout(5000, () => request.destroy(new Error("WebSocket proxy timed out")));
    request.on("upgrade", (response, socket) => { socket.destroy(); resolve(response.statusCode); });
    request.on("response", (response) => { response.resume(); resolve(response.statusCode); });
    request.on("error", reject);
    request.end();
  });
  assert.equal(await upgrade(true), 101);
  assert.equal(await upgrade(false), 403);
  assert.deepEqual(upgrades[0], { url: "/ws/signals", cookie, origin });
  const logout = await fetch(`${origin}/api/v1/auth/logout`, { method: "POST", headers: { Cookie: cookie, Origin: origin } });
  assert.equal(logout.status, 204);
  assert.match(logout.headers.get("set-cookie"), /Max-Age=0/);
  console.log("PASS: /, /review, anonymous /me, login + HttpOnly cookie, authenticated /me, WebSocket Upgrade + Cookie + Origin, anonymous WebSocket rejection, logout cookie clearing");
} finally {
  server?.kill("SIGTERM");
  for (const socket of sockets) socket.destroy();
  backend.close();
}
