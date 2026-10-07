# Supported Radar extension

In Chrome Extensions, turn on Developer mode, choose **Load unpacked**, and select this `extension/` directory. The sibling `chrome_extension/` directory is a legacy version and does not connect.

Sign in to the dashboard at `http://localhost:8080` or `http://127.0.0.1:8080` and keep that tab open. Current manifest permissions cover only those loopback addresses; this package does not support a remote HTTPS dashboard. The extension does not read the API key or session cookie. The dashboard forwards events received over its own authenticated WebSocket session through the existing bridge.

An X highlight is applied only to an article with the same numeric post ID. A post absent from the page remains in the popup alert list and is never attributed to another article. Telegram alerts appear as Telegram in the popup and link to the channel when a valid username is present.

Local tests from the project root: `node --test extension/tests/*.test.cjs`.
