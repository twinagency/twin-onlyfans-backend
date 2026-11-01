// Stage 1C part 1 cross-language harness: start Twin's REAL loopback adapter
// with a stubbed model and isolated data/log directories, hand the Python test
// its port and token, and report how many model calls and assistant history
// entries resulted.
//
// Run:  node <twin>/node_modules/tsx/dist/cli.mjs <this file>
//       (cwd = the Twin repository root)
//
// Protocol: one JSON line on stdout with {port, token}, then one final JSON
// line on shutdown with {modelCalls, assistantMessages}. Nothing else goes to
// stdout. It closes on stdin end or SIGTERM.
import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";

const twinRoot = process.env.TWIN_ROOT;
if (!twinRoot) {
  process.stderr.write("TWIN_ROOT is required\n");
  process.exit(2);
}

const j = (name) => pathToFileURL(path.join(twinRoot, name)).href;
const { config } = await import(j("src/config.ts"));
const { setAccountBindings, conversationKey } = await import(j("src/channels/identity.ts"));
const { startBridgeServer } = await import(j("src/channels/http.ts"));
const { listConversationKeys, readState } = await import(j("src/channels/store.ts"));

const TOKEN = process.env.TWIN_BRIDGE_TEST_TOKEN;
const ACCOUNT = process.env.TWIN_BRIDGE_TEST_ACCOUNT;
const CREATOR = process.env.TWIN_BRIDGE_TEST_CREATOR;
const FAN = process.env.TWIN_BRIDGE_TEST_FAN;
const REPLY = process.env.TWIN_BRIDGE_TEST_REPLY ?? "neutral stub reply";
// Provider METADATA, not an identifier: Twin accepts any nonblank string and
// projects it verbatim, so the cross-language test can ask for a label such as
// "synthetic/provider" and check that Python keeps it byte-for-byte.
const PROVIDER = process.env.TWIN_BRIDGE_TEST_PROVIDER ?? "stub";

config.girl = CREATOR;
config.dataDir = process.env.TWIN_BRIDGE_TEST_DATA;
config.logDir = process.env.TWIN_BRIDGE_TEST_LOG;
setAccountBindings([[ACCOUNT, CREATOR]]);

let modelCalls = 0;
// How many transcript entries the engine had when each generation started, in
// call order. The synthetic round trip uses this to prove a later fan turn sees
// the previous DELIVERED reply in history.
const historyLengths = [];
const generate = async (_girl, _catalogue, chatId) => {
  modelCalls += 1;
  try {
    const key = conversationKey({
      channel: "onlyfans", accountId: ACCOUNT, creatorSlug: CREATOR,
      conversationId: FAN, fanId: FAN,
    });
    historyLengths.push(chatId === key ? readState(key).transcript.length : -1);
  } catch {
    historyLengths.push(-1);
  }
  return {
    result: {
      bubbles: [REPLY], note: "ok", ms: 1, provider: PROVIDER,
      tokensIn: 1, tokensOut: 2, cost: 0, media: null, free: false,
    },
    pending: { chatId, epoch: 0, text: REPLY },
  };
};

const girl = {
  slug: CREATOR, name: "Tester", cloneId: "00000000-0000-0000-0000-000000000000",
  varsFile: "", vars: {}, prompt: "neutral", greeting: "hii", brb: ["one sec"],
  deflections: ["no thanks"],
};

const bridge = await startBridgeServer({
  token: TOKEN, girl, generate, port: 0, recover: () => undefined,
});

const expectedKey = conversationKey({
  channel: "onlyfans", accountId: ACCOUNT, creatorSlug: CREATOR,
  conversationId: FAN, fanId: FAN,
});
const assistantMessages = () => {
  let n = 0;
  for (const key of listConversationKeys()) {
    if (key !== expectedKey) continue;
    n += readState(key).transcript.filter((m) => m.role === "assistant").length;
  }
  return n;
};

process.stdout.write(JSON.stringify({ port: bridge.port, token: TOKEN }) + "\n");

let closing = false;
const shutdown = async () => {
  if (closing) return;
  closing = true;
  try {
    const assistant = assistantMessages();
    process.stdout.write(JSON.stringify({ modelCalls, assistantMessages: assistant, historyLengths }) + "\n");
    await bridge.close();
  } catch (e) {
    process.stderr.write(`shutdown failed: ${String(e && e.message ? e.message : e)}\n`);
  }
  process.exit(0);
};

process.stdin.on("end", () => void shutdown());
process.stdout.on("error", () => void shutdown());
process.on("SIGTERM", () => void shutdown());
process.on("SIGINT", () => void shutdown());
// stdin starts paused, so 'end' would never fire on its own when the parent
// closes the pipe. resume() puts it in flowing mode and the close reaches us.
process.stdin.resume();
