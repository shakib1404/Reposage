# Repo-Task Executor — কিভাবে কাজ করে

RepoSage-এর মেইন app-এ এটা **"RepoTask Exec"** নামে একটা ট্যাব — এটা মূল `backend/executor.py`-র থেকে সম্পূর্ণ আলাদা, standalone একটা agent। কাজ একটাই: একটা GitHub repo আর একটা task (বাংলায় বা ইংরেজিতে লেখা, যেমন "fix the login bug") দিলে, এটা repo clone করে, কোড পড়ে, Claude দিয়ে fix generate করে, test চালায়, আর সফল হলে সরাসরি GitHub-এ **real commit + push + Pull Request** করে দেয়।

পুরো pipeline: **Clone → Read (graph বিশ্লেষণ) → Generate fix → Apply → Test → (fail হলে retry) → Commit → Push → PR**

---

## ফোল্ডার কাঠামো ও কোন ফাইল কী করে

```
repo-task executor/
├── main.py              ← CLI entry point (run / analyze / doc / generate / serve)
├── run_task.py          ← backend/main.py যেটা subprocess হিসেবে চালায় (RepoTask Exec ট্যাব)
├── agent/
│   ├── orchestrator.py  ← মূল pipeline controller (self-healing retry loop)
│   ├── cloner.py        ← GitHub repo clone + default branch detect
│   ├── reader.py        ← তিনটা graph বানিয়ে SmartContext তৈরি করে
│   ├── claude_agent.py  ← Claude-কে কল করে, patch hunk parse করে
│   ├── writer.py        ← patch গুলো আসল ফাইলে apply করে
│   ├── tester.py        ← Docker/local-এ test চালায়
│   ├── github_pr.py     ← commit, push, PR তৈরি (secret-scrubbing সহ)
│   └── doc_agent.py     ← docstring/README/নতুন module generate করার আলাদা mode
├── graphs/
│   ├── hct.py            ← Hierarchical Code Tree — task-relevance স্কোরিং
│   ├── fcg.py             ← Function Call Graph — কে কাকে call করে
│   ├── mdg.py             ← Module Dependency Graph — import graph + PageRank risk
│   └── context_builder.py ← তিনটা graph মিলিয়ে Claude-কে পাঠানোর SmartContext বানায়
├── api/server.py         ← standalone FastAPI server (এই agent-কে নিজে থেকে চালানোর জন্য)
└── utils/                ← logger ও ছোটখাটো file helper
```

`backend/main.py`-র **`POST /api/taskexec`** route (RepoTask Exec ট্যাবের backend) সরাসরি `run_task.py`-কে subprocess হিসেবে চালায় আর stdout লাইন-বাই-লাইন SSE দিয়ে frontend-এ স্ট্রিম করে — তাই আপনি ট্যাবে যে "🚀 RepoTask Executor…" টাইপের লগ লাইন দেখেন, সেগুলো আসলে `utils/logger.py`-র `log.info()`/`log.success()` কলের raw output, redact করা (`_scrub_secrets`) টোকেন বাদে।

---

## ১. `agent/orchestrator.py` — মূল controller

এটাই পুরো pipeline-এর "brain"। `AgentOrchestrator.run(github_url, task)` একটাই public method, যেটা ধাপে ধাপে বাকি সব module কল করে।

### `run()` — ৪টা ধাপ

1. **Clone** — `RepoCloner` দিয়ে repo নামায়, একই সাথে GitHub API হিট করে default branch (main/master) বের করে রাখে (PR পরে কোন branch-এর বিরুদ্ধে খুলবে তার জন্য লাগবে)।
2. **Read & Build Graphs** — `CodeReader.read(task)` কল করে, যেটা ভিতরে HCT + FCG + MDG তিনটাই বানিয়ে একটা `SmartContext` ফেরত দেয় (নিচে বিস্তারিত)।
3. **Generate → Test loop** — `_generate_and_test_loop()` কল করে (নিচে বিস্তারিত) — এখানেই retry logic।
4. **Commit & Push PR** — যদি কোনো ফাইল সত্যিই বদলেছে, `GitHubPRAgent` দিয়ে commit/push/PR করে। ফাইল না বদলালে (যেমন task টাই ছিল "list the files in this repo" — purely informational), `git commit` এমনিতেই fail করত কারণ staged কিছু নেই — তাই এটাকে real failure না ধরে "no changes needed" হিসেবে success রিপোর্ট করে।

### `_generate_and_test_loop()` — self-healing retry loop (এই সেশনেই বাগ ফিক্স হয়েছিল এখানে)

```
for attempt in 1..max_retries:
    changes = claude.generate_fix(task, ctx, previous_error)   # Claude-কে কল
    applied = writer.apply(changes)                            # ফাইলে লিখে ফেলে
    touched += applied                                         # ← সব attempt জুড়ে accumulate
    result.changed_files = touched

    test_result = tester.run(...)
    if test_result.passed:
        return changes, test_result                            # সফল, loop থামে
    previous_error = test_result.short_summary()                # fail হলে error-সহ retry
    ctx = reader.read(task)                                     # ডিস্কের বর্তমান অবস্থা আবার পড়ে
```

**খেয়াল করার মতো ডিজাইন সিদ্ধান্ত:** `touched` লিস্টটা *সব* attempt জুড়ে accumulate হয়, কোনো একটা একক attempt-এর patch না। কারণ: ধরুন attempt 1-এ Claude বাগ ঠিক করে ফেলল এবং ফাইল লিখল, কিন্তু কোনো কারণে test এখনও fail দেখাচ্ছে (flaky বা দ্বিতীয় সমস্যা), তাহলে attempt 2-এ Claude দেখে কোড আসলে ঠিকই আছে আর একটা **খালি patch** ফেরত দেয়। যদি orchestrator শুধু "শেষ attempt-এর patch" পড়ত, তাহলে এই খালি patch দেখে ভাবত কিছুই বদলায়নি — আর attempt 1-এর real fix GitHub-এ push না করেই "No file changes were needed" বলে থেমে যেত, যদিও ফাইল সত্যিই বদলানো ছিল ডিস্কে। `touched` accumulator এই data loss আটকায়।

প্রতিটা `attempt`-এ `previous_error` আগের test failure-এর summary ধরে রাখে এবং `RETRY_SYSTEM_PROMPT`-এর (claude_agent.py) সাথে Claude-কে পাঠায়, যাতে পরের চেষ্টা আগের ভুলটা জানে।

---

## ২. `agent/cloner.py` — `RepoCloner`

- **`clone(github_url, branch=None)`** — আগের workspace থাকলে মুছে ফেলে, GitPython দিয়ে fresh clone করে। প্রাইভেট repo-র জন্য URL-এ টোকেন inject করে (`_inject_token`)।
- **`get_default_branch(github_url)`** — PR কোন branch-এর বিরুদ্ধে খুলবে সেটা না জানলে ভুল branch-এ PR যাবে, তাই এটা GitHub API থেকে আসল default branch আনে। প্রথমে authenticated চেষ্টা করে (private repo-র জন্য লাগে), fail/401 হলে **anonymous** চেষ্টা করে — কারণ dead/rate-limited token দিয়ে একটা পাবলিক repo-তেও 401 আসতে পারে, আর সেই সময় পুরোনো কোড চুপচাপ "main" ধরে নিত, যেটা master-default repo-তে ভুল base। দুটোই fail করলে সবশেষে যা actually clone হয়েছে তার branch নাম পড়ে।
- **`_inject_token` / `_safe_url`** — টোকেন URL-এ বসায়/লগ থেকে লুকায়।

---

## ৩. Graph তিনটা — "graph intelligence" লেয়ার

RepoTask Executor পুরো repo Claude-কে পাঠায় না (টোকেন বাজেট শেষ হয়ে যাবে) — বরং তিনটা graph বানিয়ে বুঝে নেয় *কোন ফাইলগুলো আসলে গুরুত্বপূর্ণ*।

### `graphs/hct.py` — Hierarchical Code Tree (HCT)

repo-র ডিরেক্টরি স্ট্রাকচার স্ক্যান করে প্রতিটা ফাইলকে task-এর সাথে relevance স্কোর দেয়।

- **`build()`** → `_walk()` রিকার্সিভলি পুরো ডিরেক্টরি ট্রি হাঁটে, `IGNORE_DIRS` (`.git`, `node_modules`, `__pycache__`...) বাদ দেয়, `SUPPORTED_EXTENSIONS`-এর বাইরে থাকা ফাইল স্কিপ করে, প্রতিটা ফাইলের কন্টেন্ট পড়ে `HCTNode`-এ রাখে।
- **`score(task)`** → `_extract_keywords(task)` task স্ট্রিং থেকে stop-word বাদ দিয়ে keyword বের করে (যেমন "Fix the login bug" → `["login", "bug"]`)। `_score_node()` প্রতিটা ফাইলের জন্য: filename-এ keyword থাকলে বেশি স্কোর (২.০), content-এ থাকলে কম স্কোর (count-based, cap করা)। `_propagate()` সেই স্কোর নিচ থেকে উপরে ডিরেক্টরিতে bubble-up করে (একটা ডিরেক্টরির স্কোর = তার সবচেয়ে ভালো child-এর স্কোর)।
- **`get_top_files(top_k)`** → স্কোর অনুযায়ী সর্ট করে টপ-K ফাইল ফেরত দেয় — এগুলোই Claude-কে পাঠানো হবে।
- **`get_structure_summary()`** → Claude-কে context দেওয়ার জন্য pretty-printed tree + score বানায়।

### `graphs/fcg.py` — Function Call Graph (FCG)

কে কাকে call করে তার directed graph (`networkx.DiGraph`)।

- **`_parse_python()`** → Python ফাইলের জন্য `ast` দিয়ে parse করে, `_PythonVisitor` প্রতিটা function definition আর তার ভিতরের call বের করে। `test_` prefix থাকা function-কে `is_test` ট্যাগ দেয়।
- **`_parse_generic()`** → non-Python ফাইলের (JS/Go/etc.) জন্য regex-ভিত্তিক fallback parser — কম নির্ভরযোগ্য কিন্তু ভাষা-নিরপেক্ষ।
- **`resolve()`** → parse করার সময় call টার্গেট (`?::name` placeholder) নিশ্চিতভাবে জানা থাকে না (ঐ নামের function কোথায় আছে, একই নামে একাধিক হতে পারে), তাই সব ফাইল পড়ার পর একটা দ্বিতীয় পাস দিয়ে সব placeholder-কে real node-এ resolve করে, যেগুলো মেলে না সেগুলো বাদ দেয়।
- **`get_impact_radius(node_ids, depth)`** → একটা function বদলালে তার caller/callee-রা (BFS দিয়ে `depth` ধাপ পর্যন্ত) ভেঙে যেতে পারে — এটাই "impact radius", Claude-কে দেখানো হয় যাতে signature বদলালে caller-ও আপডেট করে।
- **`get_test_nodes_for(node_ids)`** → কোন টেস্ট ফাইল/function এই কোডের সাথে সম্পর্কিত, সেটা বের করে — test target হিসেবে ব্যবহার হয়।

### `graphs/mdg.py` — Module Dependency Graph (MDG)

ফাইলগুলো একে অপরকে `import` করে কিভাবে — এবং PageRank দিয়ে বোঝে কোন ফাইল বদলানো "ঝুঁকিপূর্ণ"।

- **`_parse_python_imports()`** → `ast` দিয়ে `import`/`from ... import` statement বের করে, `_resolve()` relative (`.`/`..`) আর absolute import-কে আসল ফাইল পাথে ম্যাপ করে।
- **`_parse_generic_imports()`** → JS/TS/Go-এর জন্য regex-ভিত্তিক import parser।
- **PageRank** → `build()`-এর শেষে `nx.pagerank()` চালায়। যেসব ফাইল অনেক জায়গা থেকে import হয় (high PageRank), তারা "central" — একটা বদল পুরো repo জুড়ে ছড়িয়ে পড়তে পারে। `get_risk_level()` সেই স্কোরকে HIGH/MEDIUM/LOW লেবেলে ভাগ করে (থ্রেশহোল্ড ০.০৫/০.০১)। Claude-এর system prompt-এ স্পষ্ট বলা আছে: "Never freely edit HIGH-risk modules without explicitly noting the risk।"

### `graphs/context_builder.py` — তিনটা graph-কে একটা package-এ মেলানো

এটাই আসল "intelligence" — পুরো repo না পাঠিয়ে surgical subset পাঠানোর কৌশল।

- **`_smart_extract()`** — এই ফাইলের সবচেয়ে ক্লেভার অংশ। একটা ফাইল ছোট (≤১৫০ লাইন) হলে পুরোটাই পাঠায়। বড় হলে, আর সেটা Python হলে, **পুরো ফাইল না পাঠিয়ে শুধু সেই function-গুলোর body পাঠায় যেগুলো FCG অনুযায়ী relevant** (`_get_function_ranges()` দিয়ে AST থেকে প্রতিটা function-এর শুরু/শেষ লাইন বের করে), তার চারপাশে কয়েক লাইন context (`_CONTEXT_LINES`), আর ফাইলের শুরুর import/preamble অংশ (`_preamble_end()`)। যেসব অংশ বাদ পড়লো, সেখানে একটা "skipped" নোট বসিয়ে দেয় — আর Claude-কে স্পষ্ট বলে দেয় "fixed ফাইল ফেরত দেওয়ার সময় সম্পূর্ণ ফাইল দিও, শুধু অংশ না।" এভাবে একটা ৩০০০ লাইনের ফাইলের মাঝে ২০ লাইনের একটা বাগ থাকলেও, পুরো ফাইল না পাঠিয়ে টোকেন বাঁচানো যায়, আবার context miss হওয়ার ঝুঁকিও কমে (ব্লাইন্ড হার্ড কাট-অফের বদলে function-অ্যাওয়ার কাট)।
- Non-Python বা AST parse ফেল করলে — hard line-cap (`_FALLBACK_CAP_LINES`) fallback।
- **`build(task)`** — HCT স্কোর করে টপ-K ফাইল বের করে → সেই ফাইলগুলোর জন্য FCG থেকে impact radius বের করে (callers/callees) → MDG থেকে risk score টানে → test target বের করে → সব মিলিয়ে একটা `SmartContext` বানায়।
- **`SmartContext.format_for_claude()`** — এই pydantic-ঘেঁষা dataclass-টাই আসল টেক্সট ব্লক বানায় যা Claude-কে `user` message হিসেবে পাঠানো হয়: REPO STRUCTURE, RELEVANT FILES (রিস্ক ট্যাগ সহ), IMPACT RADIUS, MODULE RISK SCORES, TEST TARGETS — এই পাঁচটা সেকশনে ভাগ করা।

---

## ৪. `agent/claude_agent.py` — Claude-কে কল করা ও parse করা

এটা repo-কে পুরোপুরি rewrite না করিয়ে **patch hunk** ফরম্যাট ব্যবহার করে (`{"old": "...", "new": "..."}` জোড়া) — কারণ একটা বড় ফাইল পুরোটা আউটপুট করতে বললে Claude-এর ৮১৯২-token আউটপুট লিমিটে কেটে যাওয়ার (truncate) ঝুঁকি থাকে। হুংক ছোট রাখলে যত বড় ফাইলই হোক, আউটপুট ~৫০০ টোকেনের মধ্যে থাকে।

- **`generate_fix(task, context, previous_error)`** — প্রথম attempt-এ `SYSTEM_PROMPT` ব্যবহার করে, retry attempt-এ (`previous_error` দেওয়া থাকলে) `RETRY_SYSTEM_PROMPT` — যেটা স্পষ্টভাবে বলে "আগের attempt-এর টেস্ট ফেল করেছে, কারণ diagnose করে ঠিক patch দাও।"
- **`_parse_response()`** — Claude সবসময় পরিষ্কার JSON দেয় না, তাই তিন ধাপের চেষ্টা: (১) সরাসরি ```json ... ``` fence স্ট্রিপ করে parse, (২) direct `json.loads()`, (৩) দুটোই ফেল করলে **brace-counting fallback** — টেক্সটে `{` খুঁজে, bracket depth গুনে গুনে প্রথম সম্পূর্ণ balanced JSON object বের করে parse করে। সবই fail করলে তখনই exception তোলে।
- **`_make_changes(data)`** — raw dict-কে `CodeChanges` dataclass-এ বদলায়। `files` ফিল্ডে প্রতিটা এন্ট্রি list (patch hunks) হলে `Hunk` অবজেক্টে বদলায়, string হলে (লিগ্যাসি "পুরো ফাইল rewrite" ফরম্যাট) সরাসরি রাখে — দুটো ফরম্যাটই সাপোর্ট করে পেছনের compatibility-র জন্য।

---

## ৫. `agent/writer.py` — `CodeWriter`

Claude-এর দেওয়া `CodeChanges`-কে আসল ফাইলে প্রয়োগ করে।

- **`apply()`** — তিন ধরনের কাজ: existing file edit (`files`), নতুন ফাইল তৈরি (`new_files`, পুরো content লিখে ফেলে), ফাইল ডিলিট (`deleted_files`)। যেসব path বদলেছে তাদের লিস্ট ফেরত দেয় (`changed_paths`) — এটাই পরে `touched` accumulator-এ যায় orchestrator-এ।
- **`_apply_patches()`** — প্রতিটা hunk-এর `old` স্ট্রিং ফাইলের মধ্যে exact match খোঁজে, পেলে `new`-এ replace করে। Exact match না পেলে line-ending normalize করে (`\r\n` → `\n`) আরেকবার চেষ্টা করে, কারণ Windows-style line ending বনাম Unix-style line ending-এর পার্থক্যে একটা আসলে-সঠিক hunk ভুলভাবে "not found" দেখাতে পারত। তাও না মিললে warning লগ করে ওই hunk বাদ দেয় (পুরো task fail করায় না, বাকি hunk গুলো তবু apply হয়)।

---

## ৬. `agent/tester.py` — `TestRunner`

Fix apply করার পর test suite চালানো, **Docker sandbox-এ** (নিরাপদ, isolated), Docker না থাকলে local subprocess-এ fallback।

- **`_detect_language()`** — repo root-এ `requirements.txt`/`package.json`/`go.mod`/ইত্যাদি দেখে ভাষা অনুমান করে।
- **`run(custom_commands)`** — Claude যদি নিজে কোনো test command সাজেস্ট করে (`test_commands` ফিল্ড) সেটাই চালায়, নাহলে ভাষা অনুযায়ী ডিফল্ট কমান্ড (`TEST_COMMANDS` ডিক্ট) ব্যবহার করে।
- **`_run_docker()`** — `docker run --rm --memory=1g --cpus=2` দিয়ে একটা ভাষা-নির্দিষ্ট ইমেজে (`DOCKER_IMAGES` ডিক্ট) ওয়ার্কস্পেস মাউন্ট করে টেস্ট চালায় — মেমরি/CPU ক্যাপ দেওয়া আছে যাতে arbitrary agent-generated কোড পুরো মেশিন খেয়ে না ফেলে। Docker daemon না থাকলে বা নিজেই error দিলে (`permission denied`, `Cannot connect` ইত্যাদি স্ট্রিং মিলিয়ে) `_run_local()`-এ fallback করে।
- **`has_tests()`** — repo-তে `test_*`/`*_test.py`/`spec.js` প্যাটার্নের ফাইল আছে কিনা চেক করে; না থাকলে orchestrator টেস্ট ধাপ সম্পূর্ণ স্কিপ করে (`run_no_tests()` — একটা dummy passing result)।

---

## ৭. `agent/github_pr.py` — `GitHubPRAgent`

Commit, push, আর real GitHub PR তৈরি করে।

- **`commit_and_push()`** — slug করা branch নাম বানায় (`_branch_name` — task টেক্সট থেকে lowercase-hyphenated slug + timestamp), নতুন branch checkout করে, বদলানো ফাইলগুলো stage করে commit করে, তারপর push করে। পুশের URL ফরম্যাট গুরুত্বপূর্ণ: `https://x-access-token:<token>@github.com/...` — শুধু `https://<token>@...` দিলে git এটাকে ইউজারনেম ধরে নেয়, পাসওয়ার্ড ছাড়া, আর TTY না থাকায় prompt করতে গিয়ে **raw token-সহ এরর মেসেজ** ছাপিয়ে ফেলে (`fatal: could not read Password for 'https://ghp_...@github.com'`) — GitHub-এর ডকুমেন্টেড ফিক্স হলো fixed username + token-as-password।
- **`scrub_secrets()`** — git push fail করলে পুরো remote URL (টোকেন-সহ) error message-এ echo হয়ে যায় — এটা একবার সত্যিই ঘটেছিল আর raw token web UI-তে দেখা গিয়েছিল (কোড কমেন্টে লেখা আছে)। তাই raise করার আগে regex দিয়ে `ghp_`/`gho_`/`github_pat_`/`https://user:pass@` প্যাটার্ন সবসময় রিডact করা হয়।
- **`create_pr()`** — GitHub API দিয়ে আসল PR খোলে। PR তৈরি fail হতে পারে (ঐ branch-এ আগেই PR আছে, বা token-এর পর্যাপ্ত scope নেই) — এই ক্ষেত্রে push করা branch-টা কাজে লাগানোর জন্য টিকে থাকে, কিন্তু রিটার্ন ভ্যালুতে স্পষ্ট লেখা থাকে "(no PR — branch only) https://.../tree/branch" — যাতে caller ভুলভাবে এটাকে successful PR URL হিসেবে না দেখায়।

---

## ৮. `agent/doc_agent.py` — আলাদা তিনটা mode (CLI-only, মূল fix-loop-এর বাইরে)

`main.py`-র `doc`/`generate` CLI কমান্ড থেকে ব্যবহার হয় (RepoTask Exec ট্যাব থেকে নয়, এটা শুধু CLI):

- **docstring mode** (`generate_docstrings`) — `_has_undocumented()` দিয়ে AST ঘুরে দেখে কোন function/class-এর docstring নেই, সেগুলো `ClaudeCodeAgent`-এর মতো একই patch-hunk ফরম্যাটে fix করে।
- **README mode** (`generate_readme`) — repo structure + টপ-৫ গুরুত্বপূর্ণ ফাইল + `requirements.txt`/`setup.py` পড়ে একটা পুরো README.md generate করে।
- **module mode** (`generate_module`) — টপ-৩ ফাইলকে "স্টাইল উদাহরণ" হিসেবে দিয়ে, repo-র কনভেনশন মিলিয়ে একটা সম্পূর্ণ নতুন মডিউল লিখে দেয়।

সবগুলোই `ClaudeCodeAgent._make_changes`/`_parse_response`-এর মতোই parse লজিক শেয়ার করে (`doc_agent.py`-তেই ডুপ্লিকেট করা আছে)।

---

## ৯. `api/server.py` — standalone FastAPI সার্ভার

এটা RepoSage-এর মূল backend (`backend/main.py`) থেকে আলাদা — repo-task executor-কে **নিজে থেকে একটা independent সার্ভিস হিসেবে** চালানোর অপশন (`python main.py serve`)।

- **`POST /run`** — ব্যাকগ্রাউন্ড থ্রেডে `AgentOrchestrator.run()` চালু করে, সাথে সাথে একটা `job_id` ফেরত দেয়।
- **`GET /job/{job_id}`** — ইন-মেমরি ডিক্ট (`_jobs`) থেকে status/result পোল করা।
- **`POST /analyze`** — শুধু clone + graph build করে কোন ফাইল টাচ হবে দেখায়, কোনো এডিট ছাড়া (dry-run)।

RepoSage-এর নিজের UI (RepoTask Exec ট্যাব) এই সার্ভারটা ব্যবহার করে না — বরং `run_task.py`-কে সরাসরি subprocess হিসেবে চালায় (উপরে main.py integration সেকশন দেখুন)। এই `api/server.py` আলাদাভাবে ব্যবহার করতে চাইলেই লাগে।

---

## এক নজরে — পুরো ডেটা-ফ্লো

```
GitHub URL + Task
      │
      ▼
RepoCloner.clone()  ──────────► লোকাল workspace/ ফোল্ডারে ফ্রেশ clone
      │
      ▼
CodeReader.read(task)
      ├─ HCT.build().score(task)      → টপ-K relevant ফাইল (keyword match)
      ├─ FCG.build().resolve()        → caller/callee গ্রাফ, impact radius
      ├─ MDG.build()                  → import গ্রাফ + PageRank risk
      └─ GraphContextBuilder.build()  → SmartContext (surgical subset, token-বাজেট মেনে)
      │
      ▼
_generate_and_test_loop()  (max_retries বার পর্যন্ত)
      ├─ ClaudeCodeAgent.generate_fix()  → patch hunks (JSON)
      ├─ CodeWriter.apply()              → ডিস্কে লেখে, touched[] জমা হয়
      └─ TestRunner.run()                → pass হলে থামে, fail হলে error নিয়ে retry
      │
      ▼  (touched না খালি হলে)
GitHubPRAgent.commit_and_push() → নতুন branch, commit, push (secret-scrubbed)
GitHubPRAgent.create_pr()       → আসল GitHub Pull Request
```
