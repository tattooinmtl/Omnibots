// Dev-time only: run Omni's OWN toolcalls.mjs on a fixed set of inputs and
// write tests/goldens/toolcalls.json. The Python port (omnibots/providers/
// toolcalls.py) must reproduce every result exactly.
//
// Usage: node tools/gen_toolcall_goldens.mjs [omni install root] [--check]

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const args = process.argv.slice(2);
const check = args.includes("--check");
const root = path.resolve(args.find((a) => !a.startsWith("--")) || path.join(process.env.USERPROFILE || process.env.HOME, ".omni"));
const here = path.dirname(fileURLToPath(import.meta.url));
const out = path.join(here, "..", "tests", "goldens", "toolcalls.json");
process.chdir(root);
const imp = (p) => import(pathToFileURL(path.join(root, p)).href);
const tc = await imp("src/core/toolcalls.mjs");
const { tools } = await imp("src/tools/index.mjs");

// Registry source: Omni's real tool schemas (names + parameter types only).
const toolDefs = tools.map((t) => ({
  type: "function",
  function: {
    name: t.function.name,
    parameters: { type: "object", properties: Object.fromEntries(
      Object.entries(t.function.parameters?.properties || {}).map(([k, v]) => [k, { type: v?.type || "" }])) },
  },
}));
const reg = tc.buildParamRegistry(toolDefs);

const parserSnippet = [
  "// Does the content look like the model attempted a tool call at all?",
  "export function hasToolIntent(content) {",
  "  return /<tool_call|<function\\s*=|<arg_key>|<parameter\\s*=|<invoke\\b/i.test(String(content || \"\"));",
  "}",
  "export function stripToolCallText(content) {",
  "  let s = String(content || \"\");",
  "  s = s.replace(/<tool_call>[\\s\\S]*?<\\/tool_call>/g, \"\");",
  "  s = s.replace(/<function\\s*=[^>]*>[\\s\\S]*?(?:<\\/function>|$)/g, \"\");",
  "  return s;",
  "}",
].join("\n");

// Omni's own regression inputs (tests/toolcalls.test.mjs) + extra edge cases.
const PARSE = [
  `Inspecting now.<tool_call>list_dir<path>omni-todos</parameter>\n<parameter=recursive>true</parameter>\n</function>`,
  `<tool_call>\n<function=read_file>\n<parameter=path>src/index.js</parameter>\n<parameter=limit>100</parameter>\n</function>\n</tool_call>`,
  `<tool_call>search\n<arg_key>pattern</arg_key>\n<arg_value>TODO</arg_value>\n<arg_key>case_insensitive</arg_key>\n<arg_value>true</arg_value>`,
  `<tool_call>\n{"name": "run_shell", "arguments": {"command": "npm test", "timeout_ms": 60000}}\n</tool_call>`,
  `<function=git_status>\n</function>`,
  `<tool_call>\n<function=list_dir>\n{"path": "src"}\n</function>\n</tool_call>`,
  `<tool_call><function=git_status></function></tool_call>\n<tool_call><function=list_dir><parameter=path>.</parameter></function></tool_call>`,
  `<tool_call>\n<function=write_file>\n<parameter=path>cfg.json</parameter>\n<parameter=content>{"a": 1}</parameter>\n</function>\n</tool_call>`,
  `<tool_call><function=edit_file><parameter=path>a.html</parameter><parameter=old_string><div>x</div></parameter><parameter=new_string><div>y</div></parameter></function></tool_call>`,
  `<think>plan first</think><tool_call><function=list_dir><parameter=path>.</parameter></function></tool_call>`,
  "The bug is in line 42.",
  `<tool_call><invoke name="read_file"><parameter=path>x.js</parameter></invoke></tool_call>`,
  `<tool_call>\n<function=write_file>\n<parameter=path>a.txt</parameter>\n<parameter=content>line1\nline2 </parameter> tricky</parameter>\n</function>\n</tool_call>`,
  `<tool_call>\n{"name": "write_file", "arguments": {"path": "y.js", "content": "a </parameter> b"}}\n</tool_call>`,
  `<tool_call>\n{"name": "write_file", "arguments": {"path": "x.js", "content": "alpha</tool_call> beta"}}\n</tool_call>`,
  `<tool_call>\n<function=write_file>\n<parameter=path>b.txt</parameter>\n<parameter=content>foo </tool_call> bar</parameter>\n</function>\n</tool_call>`,
  `<tool_call>\n<function=write_file>\n<parameter=path>src/core/toolcalls.mjs</parameter>\n<parameter=content>${parserSnippet}</parameter>\n</function>\n</tool_call>`,
  `<tool_call>\n<function=write_file>\n<parameter=path>c.txt</parameter>\n<parameter=content>line1\nliteral \\</parameter>\nline3</parameter>\n</function>\n</tool_call>`,
  `<tool_call>\n<function=write_file>\n<parameter=path>d.txt</parameter>\n<parameter=content>alpha \\</tool_call>\nomega</parameter>\n</function>\n</tool_call>`,
  // extras
  `<tool_call>\r\n<function=read_file>\r\n<parameter=path>win.txt</parameter>\r\n</function>\r\n</tool_call>`,
  `<tool_call>\n<function=READ_FILE>\n<parameter=path>case.txt</parameter>\n</function>\n</tool_call>`,
  `<tool_call>\n<function=totally_unknown_tool>\n<parameter=x>1</parameter>\n<parameter=y>[1,2]</parameter>\n</function>\n</tool_call>`,
  `<tool_call>\n<function=read_file>\n<parameter=path>a</parameter>\n</function>\n</tool_call>\n<tool_call>\n<function=read_file>\n<parameter=path>b</parameter>`,
  `<tool_call>\n{"name": "read_file", "arguments": "{\\"path\\": \\"stringified.txt\\"}"}\n</tool_call>`,
  `<tool_call>\n<function=run_shell>\n<parameter=command>grep '\\<word\\>' f.txt</parameter>\n</function>\n</tool_call>`,
  `<tool_call>\n<function=write_file>\n<parameter=path>unicode.txt</parameter>\n<parameter=content>héllo — 世界 🎉</parameter>\n</function>\n</tool_call>`,
  `<think>unclosed reasoning <tool_call><function=list_dir><parameter=path>.</parameter></function></tool_call>`,
  `Some prose. <function=git_status></function> more prose`,
  ``,
];

const SPLIT = [
  ["The bug is ", "in line 42."],
  ["<think>plan first</think>Here is the fix."],
  ["prefix ", "<th", "ink>reasoning here</th", "ink> answer text"],
  ["<think>step one step two", "</th", "ink>done"],
  ["<think>a</think>mid<think>b</think>end"],
  ["intro<think>never closes"],
  ["<", "t", "h", "i", "n", "k", ">", "x", "<", "/", "think>y"],
];
const THINK = ["<think>because X</think>The answer is Y.", "just an answer", "intro<think>cut off mid-thought", "<think>a</think>b<think>c"];
const STRIP_TC = [`Before.<tool_call>x</tool_call>After.<tool_call>unclosed...`, `a<function=x>y</function>b<function=z>unclosed`, "plain"];
const INTENT = ["<tool_call>garbage", "normal text", "<FUNCTION = x>", "<invoke name=\"a\">", "<arg_key>k"];

const goldens = {
  omni_version: JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8")).version,
  tool_defs: toolDefs,
  parse: [
    ...PARSE.map((input) => ({ input, registry: true, calls: tc.parseTextToolCalls(input, reg).map((c) => ({ name: c.function.name, args: JSON.parse(c.function.arguments) })) })),
    ...PARSE.map((input) => ({ input, registry: false, calls: tc.parseTextToolCalls(input, null).map((c) => ({ name: c.function.name, args: JSON.parse(c.function.arguments) })) })),
  ],
  split: SPLIT.map((chunks) => {
    const s = tc.createThinkSplitter();
    let think = "", answer = "";
    for (const c of chunks) { const r = s.feed(c); think += r.think; answer += r.answer; }
    const r = s.flush(); think += r.think; answer += r.answer;
    return { chunks, think, answer };
  }),
  extract_think: THINK.map((input) => ({ input, ...tc.extractThink(input) })),
  strip_think: THINK.map((input) => ({ input, output: tc.stripThink(input) })),
  strip_tool_call_text: STRIP_TC.map((input) => ({ input, output: tc.stripToolCallText(input) })),
  has_tool_intent: INTENT.map((input) => ({ input, output: tc.hasToolIntent(input) })),
  text_tool_instructions: tc.textToolInstructions(toolDefs.slice(0, 3)),
  recovery_message: tc.recoveryMessage(),
};

const text = JSON.stringify(goldens, null, 1) + "\n";
if (check) {
  const cur = fs.existsSync(out) ? fs.readFileSync(out, "utf8").replace(/\r\n/g, "\n") : "";
  if (cur !== text) { console.error("tests/goldens/toolcalls.json is out of date with Omni; run: node tools/gen_toolcall_goldens.mjs"); process.exit(1); }
  console.log("toolcall goldens match Omni");
} else {
  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, text);
  console.log(`wrote ${out}: ${goldens.parse.length} parse cases, ${goldens.split.length} split cases (Omni ${goldens.omni_version})`);
}
process.exit(0);
