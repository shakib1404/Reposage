# Copy Detector — কিভাবে কাজ করে

RepoSage-এর Copy Detector আসলে **তিনটা আলাদা detection engine**-এর উপর বসানো একটা UI — প্রতিটা আলাদা প্রশ্নের উত্তর দেয়। তিনটাই `POST /api/copydetect` বা `/api/corpus/*` route দিয়ে চলে ([`backend/main.py`](../backend/main.py))।

| Mode | প্রশ্ন | Engine | Module |
|---|---|---|---|
| **Two Repos** | এই repo কি ওই repo থেকে copy করেছে? | Token fingerprint matching (vendetect) | `backend/copydetector/` |
| **One Repo** (self-scan) | এই repo-র ভিতরেই কোনো function duplicate আছে? | Semantic embedding similarity | `backend/dupdetect.py` |
| **Corpus** | এই repo কি আগে স্ক্যান করা অন্য কোনো repo-র সাথে মেলে? | FAISS vector search | `backend/corpus.py` |

নিচে প্রতিটা engine আলাদাভাবে, এবং আপনি যে `detector.py` ফাইলটা খুলেছিলেন সেটার প্রতিটা অংশ ব্যাখ্যা করা হলো।

---

## ১. Two Repos মোড — Token Fingerprint Matching (`copydetector/`)

এটা **ঠিক text-match না** — variable rename, comment বদল, formatting বদল করলেও ধরে ফেলে, কারণ এটা tokenized k-gram fingerprint দিয়ে তুলনা করে (winnowing algorithm, third-party `copydetect` লাইব্রেরি দিয়ে — `copydetector/copydetect.py` তার wrapper মাত্র)।

### `detector.py` ফাইলের প্রতিটা অংশ

**`get_lexer_for_filename()`** — Pygments দিয়ে ফাইলের extension থেকে ভাষা বোঝার চেষ্টা করে (`.py` → Python lexer)। না পেলে `None`, আর সেই ফাইল স্কিপ হয়ে যায় — কারণ fingerprint বানাতে হলে code-কে token-এ ভাঙতে হয়, আর সেটা ভাষা-নির্ভর।

**`Status` ক্লাস** — একটা callback interface, খালি default implementation। `main.py`-তে `_SSEStatus` এটাকে override করে, যাতে scan চলাকালীন progress (`"Comparing 42 file pair(s)…"`) live frontend-এ স্ট্রিম হয়।

**`Source` dataclass** — একটা ফাইলের ভিতরে কোথায় (কোন byte range) copy পাওয়া গেছে সেটা ধরে রাখে। `byte_offset_slice_to_lines_slice()` byte offset-কে line number-এ বদলায় (UI-তে "line 42-50" দেখানোর জন্য)। `to_str()` সেটাকে মানুষ-পড়ার-মতো স্ট্রিং বানায় (`file.py:42-50`), GitHub URL হলে `#L42-L50` format ব্যবহার করে।

**`Detection` dataclass** — একটা match-এর পুরো রেকর্ড: কোন test file, কোন source file, আর `Comparison` object (overlap score)। `__lt__` দিয়ে sortable — সবচেয়ে বেশি similarity প্রথমে আসে (`comparison.py`-তে দেখুন, বেশি similarity মানে "ছোট" — heap-এ min-heap দিয়ে max বের করার trick)।

**`VenDetector`** — আসল ইঞ্জিন। তিনটা মূল method:

- **`compare(test_files, source_files)`** — দুই দিকের file list নেয়, প্রতিটা ফাইলের জন্য lexer চেক করে (না থাকলে skip, warning লগ করে যে কতগুলো suffix বাদ পড়লো), প্রতিটা file-এর fingerprint বানায় (`_get_fingerprint`, cache করা থাকে যাতে একই file বারবার না লাগে), তারপর **N×M সব জোড়া** compare করে (`comparator.compare(fp1, fp2)`)। ফলাফল একটা heap-এ জমা হয় যাতে sort করা থাকে। `incremental=True` হলে batch শেষে sorted ফলাফল yield করে, না হলে সব batch শেষে একবারে।

  খেয়াল করুন — `explored_sources` সেট দিয়ে একই test-file-এর একাধিক match থেকে শুধু সবচেয়ে ভালোটাই রাখা হয় (একটা ফাইলের একটা অংশ দুইবার "duplicate" হিসেবে না দেখানোর জন্য)।

- **`find_probable_copy(detection)`** — এটা মজার অংশ: শুধু "copy হয়েছে" বলা না, **কোন commit-এ copy হয়েছিল** সেটা বের করার চেষ্টা করে। Git history-তে পিছনে হাঁটে (`previous_version()` দিয়ে দুই repo-র আগের commit-এ গিয়ে আবার compare করে), যতক্ষণ similarity কমতে থাকে ততক্ষণ পিছাতে থাকে। `history` সেট দিয়ে loop এড়ানো হয়। **`max_history_depth`** বাউন্ড না দিলে এটা অসীম চলতে পারে — `main.py`-র কমেন্টে লেখা আছে, `psf/requests` বনাম তার fork-এ একটা detection-এ ৮ মিনিট আটকে ছিল, তাই default depth **২**-এ বাঁধা (`COPYDETECT_MAX_HISTORY_DEPTH` env var দিয়ে বদলানো যায়)।

- **`detect(test_repo, source_repo, file_filter)`** — top-level entry point: দুই repo থেকে ফাইল লিস্ট বানায় (`file_filter` দিয়ে শুধু নির্দিষ্ট extension রাখা যায়), `compare()` চালায়, প্রতিটা detection-এর জন্য `find_probable_copy()` দিয়ে commit বের করার চেষ্টা করে, তারপর yield করে।

**`@callback` ডেকোরেটর** — একটু চালাক কৌশল: কোনো method-কে (যেমন `compare`) wrap করে, call করার আগে `status.on_compare()` কে knowledge দেয় (চাইলে arguments বদলে দিতে পারে — যেমন file list filter করা), call করার পরে `status.compare_completed()` ডাকে। Method generator হলে (`yield` থাকলে) ঠিকমতো `yield from` করে pass-through করে।

### সহায়ক ফাইল

- **`comparison.py`** — `Slice` (byte range), `Comparison` (দুই ফাইলের মিলের ফলাফল: `token_overlap`, `similarity1`/`similarity2`, কোথায় মিলেছে), আর `Comparator` abstract base class (কাস্টম algorithm চাইলে subclass করা যায়)।
- **`copydetect.py`** — `CopyDetectComparator`: আসল fingerprinting (`CodeFingerprint(k=25, win_size=1)`) আর compare (`compare_files`) third-party `copydetect` প্যাকেজ থেকে কল করে, `Comparison` object-এ wrap করে।
- **`repo.py`** — `Repository`/`File`/`RemoteGitRepository` ইত্যাদি: local path বা GitHub URL থেকে repo load করা, `git clone` করা (context manager দিয়ে — `with repo:` ঢুকলে clone হয়, বেরোলে temp dir মুছে যায়), file list বের করা (`git ls-files`), আগের commit বের করা।
- **`diffing.py`** — দুইটা code snippet-এর side-by-side diff বানায় (`difflib.ndiff` ব্যবহার করে), লম্বা identical-line সিকোয়েন্স থাকলে সেটাকে `<N identical lines...>` বলে ভাঁজ করে দেখায় — UI-তে "Expand" করে দুই পাশের code দেখার ফিচারটা এটাই।

### কিভাবে জোড়া লাগে (`main.py`)

`/api/copydetect` (mode `"pair"`) — দুই repo একসাথে clone করে (parallel, `ThreadPoolExecutor` দিয়ে — কারণ clone-ই বেশিরভাগ সময় খায়), `VenDetector.detect()` চালায়, `_detection_to_dict()` প্রতিটা detection-কে JSON বানায় (line number, দুই পাশের আসল code snippet, similarity score) এবং `min_similarity`-র নিচে হলে বাদ দেয়, SSE দিয়ে স্ট্রিম করে frontend-এ।

---

## ২. One Repo মোড — Semantic Self-Scan (`dupdetect.py`)

এটা token-matching না, **embedding similarity**। একই class বা module-এর ভিতরে দুইটা function-এর **কাজ একই রকম কিনা** সেটা মাপে, even যদি variable name, comment, বা হালকা logic rewrite করা হয়।

- **`_extract_functions()`** — AST দিয়ে ফাইল পার্স করে, প্রতিটা function/method বের করে, কোন class-এর ভিতরে (নাকি module-level) সেটা মনে রাখে।
- **`_is_trivial()`** — stub body (`pass`, `...`, `raise NotImplementedError`) বাদ দেয়, নাহলে একটা abstract method-এর দশটা override সব "duplicate" দেখাতো।
- **`_get_embedder()`** — একই `all-MiniLM-L6-v2` model যেটা `rag.py`/`search.py` ব্যবহার করে (লোড একবারই হয়, module-level cache)।
- `find_semantic_duplicates()` — repo clone করে, প্রতিটা function embed করে, **একই class/module**-এর ভিতরের জোড়াগুলো তুলনা করে (ইচ্ছাকৃতভাবে class-local scope — ক্রস-class মিল সাধারণত শুধু shared boilerplate, আসল duplication smell না), `DEFAULT_THRESHOLD = 0.68`-এর বেশি হলে report করে (সত্যিকারের duplicate ০.৭১-০.৮৯ score পেয়েছিল, অসম্পর্কিত জোড়া ≤০.৪৬-এ ছিল — তাই calibrate করা)।

`/api/copydetect` (mode `"self_scan"`) সরাসরি এটা কল করে।

---

## ৩. Corpus মোড — FAISS Vector Search (`corpus.py`)

এটা প্রশ্ন করে: *"এই repo-র কোনো function কি আগে স্ক্যান করা অন্য কোনো repo-তে আছে?"* — `dupdetect.py`-র same AST extraction + embedding ব্যবহার করে, কিন্তু একটা **persistent, ক্রমবর্ধমান FAISS index**-এর বিরুদ্ধে (`IndexFlatIP`, cosine similarity, ডিস্কে `index.faiss` + `meta.json` হিসেবে টিকে থাকে)।

- **`add_to_corpus(repo)`** — repo clone → function বের করে → embed করে → index-এ যোগ করে। একবার যোগ হলে সেই repo চিরকাল corpus-এর অংশ।
- **`search_corpus(repo, threshold, top_k)`** — নতুন repo embed করে, index-এ search করে, `DEFAULT_MATCH_THRESHOLD = 0.75`-এর বেশি match থাকলে রিপোর্ট করে।
- **`_is_substantial()`** — খুব ছোট/generic function (যেমন `def __len__(self): return len(self.data)`) বাদ দেয় — নাহলে false attribution হতো (হাজারো repo-তে একই boilerplate থাকে)।

---

## এক নজরে — কোন mode কখন

```
আপনার কাছে দুইটা repo, জানতে চান একটা অন্যটা থেকে copy কিনা
            → Two Repos (token fingerprint, কোন commit-এ copy হয়েছিল তাও বলে)

আপনার কাছে একটা repo, ভিতরেই copy-paste আছে কিনা জানতে চান
            → One Repo (semantic self-scan, token মিলুক বা না মিলুক)

আপনার কাছে একটা repo, আগে স্ক্যান-করা হাজারো repo-র বিরুদ্ধে চেক করতে চান
            → Corpus (FAISS vector search, persistent index)
```
