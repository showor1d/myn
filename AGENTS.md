# MYN: current features and development rules

This is the maintained reference for source version **0.49.2**, deployed at `D:\MYN`. Verify behavior against current code and tests.

## Documentation and language

Update this document with every change to functionality, help, data structures, startup or maintenance. Delete superseded text and replace the relevant section in place. Never append patch notes, dated entries, version chapters or contradictory explanations. Replace verification results with current evidence; detailed history belongs in separate backups and logs. Give each feature one primary explanation.

UI labels, built-in messages, help, prompts, comments and documentation use English. Retain Chinese/Japanese input patterns, phonetic wake spellings and language test fixtures needed for recognition. Preserve original messages, names, personal files and saved data. Input rules and legacy markers are not display labels. Generated conversation follows the user's language and instructions.

Distinguish isolated tests, deployment checks and actual external-service/device validation. Mock models and synthetic audio do not establish live service or recognition quality.

## Startup and deployment

The desktop `MYN.lnk` launches `C:\Program Files\Python39\pythonw.exe D:\MYN\src\myn_gui.py`, working directory `D:\MYN`, without a console. Tk owns the main window; an independent WebView2 child renders the preferred UI using JSON messages, with Tk fallback. Network, model and speech work must not block the event loop.

`local-agent` is the background-task environment. `local-tts` contains Python 3.12 and Qwen3-TTS; `local-stt` contains Whisper. These task/speech dependencies remain. No local chat model is bundled: Qwen chat weights, Ollama runtime and old ensure/start/stop Ollama launchers were removed. The optional chat adapter never installs, downloads, starts, probes or selects a model automatically. Qwen3-TTS is separate speech synthesis.

Historical `versions/0.13.1`, installers and backups are not current entry points. Do not run old uninstallers or `maintenance/cleanup_legacy.ps1` against the source deployment. Preserve old backups. Build an EXE installer only on an explicit user request; routine work modifies and verifies source.

## User features

### Conversation and interface

Claude Opus 5.5 is the fixed main model. Ordinary messages are saved verbatim before routing; substantive work receives deeper reasoning, longer context and more tool rounds. Replies stream, with long text, images, screen content and annotations in the main reader. Time, recent conversation, profile and preferences supply context.

The core's left arc contains Agents, Expenses, music and keyboard. An alarm entry appears for alarms within 24 hours; WebView2 also has an equalizer entry. Main/small cores share positioning and scale. Panels stay inside the main window and the small core returns to the main view. Reduced motion remains supported.

Agents is one scrolling voice/device/API-key page. No gear menu or separate memory, backup, profile, evolution or task settings pages remain; manage those conversationally and display results in the existing reader. Keyboard opens input; Esc collapses it. `core/chat.py:HELP` owns current help.

### Originals, memory, profile and preferences

Archive ordinary input before replying, then extract memories by scene. Compatibility commands remain in conversation history but bypass the sent_messages extraction queue. Credentials use masked dedicated settings, never chat archives.

Facts retain source quotations/timestamps. Inferences are separate and excluded from factual retrieval by default. Message time is not event time; plans or guesses do not prove completion. Profiles summarize traits, retain revisions and prioritize corrections, avoiding timestamped quotation logs. Preferences apply by scene and retain feedback, examples, history and effectiveness. One-time instructions do not automatically become permanent rules.

Natural queries cover originals, active memories, trash, profile and preferences with ranges, pagination and source details. Memory edits, category/type changes, trash and restoration require exact target reading, preview, actual confirmation and fresh-state verification. No unattended edits, arbitrary SQL, bulk deletion or permanent memory deletion. Keep compatibility commands `recent`, `today`, `week`, `month`, `category`, `trash`. Do not restore retired standalone search/semantic/summary/stats or Brain/CMD HUD. Conversational web search remains separate.

### Speech and quiet scenes

Voice input, always-on microphone, automatic learning, chat preferences and automatic reading stay enabled; old disabled settings cannot override this. Hold right Ctrl or the microphone button, or say **Hey, Mine**. Acoustic wake detection and personalized training remain. Continued conversation uses context to identify speech addressed to MYN.

Whisper large-v3-turbo runs in a separate Python 3.12 process under `local-tts/.venv`. Preserve Chinese/English/Japanese recognition, pauses, echo filtering, interruption and device selection. Output uses ElevenLabs or local Qwen3-TTS in `local-tts/model`, without automatic provider fallback. Long replies read the opening unless full reading is requested. New turns and interruptions invalidate old audio and late synthesis.

Sleep, study, singing and nearby conversations reduce interruptions while direct requests remain available. Pitch/activity is a hint, not proof of singing or speaker identity; lyrics are not user facts. Quiet scenes defer proactive reports, speech and automatic restarts; alarms retain their independent exception. Spectrum display does not cause extra audio retention/uploads.

The core shows measured microphone and WASAPI loopback spectra across 128 bands. Reuse microphone capture; without spectra use only actual volume, never fabricated peaks. Clear stale/disconnected data, release devices on close and do not alter Windows default routing.

### Media and audio console

Music, Audio, Video and Movies share a local library with shuffle, lyrics, playback and resume. Scrolling is independent and navigation does not stop playback. The music alarm button sets a default sound without creating an alarm. Alarm volume is independent of music volume/mute. New alarms inherit the current default unless specified; an explicit empty sound uses speech. Changing defaults does not modify existing alarms.

The console provides six-band music/microphone EQ, gain, limiting, pitch, robot and telephone effects. Only music/audio use its media processing chain; video, movies, MYN speech and alarms do not. Microphone processing, monitoring and virtual sending require activation and do not resume automatically after restart. STT keeps its original input. Never report successful sending to a missing device. VB-CABLE is not installed; cross-app routing requires a separately installed/selected virtual device. Local EQ and monitoring work independently.

### Calendar, alarms, tasks and reports

Natural language manages MYN's calendar, reminders, alarms, countdowns and scheduled tasks in Asia/Tokyo time. Alarms and ordinary events/reminders have separate persistence/presentation to prevent duplicates. Due, finished, cancelled and recovered states follow their records. Confirm calendar deletion and sensitive scheduled actions at task creation.

Background tasks use Claude Agent SDK with progress/cancellation. Default per-task budget is 0 (unlimited), adjustable in chat. Ordinary and risky commands have different permission requirements. Evolution points support review, discussion, adoption and manual completion. Adopted plans authorize automatic backup, deployment and idle restart after verification; unadopted suggestions cannot enable themselves.

Reports draw evidence from originals, preferences, profiles and development changes. They support subscriptions, pause/resume/cancel, deferral and paginated details. Reports differ from alarms and scheduled messages. Clarify ambiguous schedules, prepare only the latest missed report, defer invitations during quiet scenes and avoid repeated interruptions.

### Web, permissions, backups and expenses

The replaceable `web_tools` layer defaults to official Claude search with user-managed limits. Screen/images, windows, shortcuts, volume, media keys, URLs and system settings use existing tools/permissions. File reading requires specific folder grants. New permissions, folder grants and confirmation-policy changes require actual confirmation. Chat claims do not establish consent. Chat tools cannot delete files or run arbitrary commands; background work uses separate risk checks.

Full backup requests open local file/password dialogs. AES-256-GCM/PBKDF2 protects personal data and durable settings; passwords require eight characters minimum. Validate imports and preserve recovery snapshots. Credentials use DPAPI/environment variables, never chat echo or ordinary settings. Never send backup passwords to models.

Expenses shows tokens, USD and categorized spending with monthly/yearly views, receipts and manual entry/correction. Mark estimates and sources. Sync Claude, ElevenLabs and registered development sessions without double-counting cache/reasoning tokens. `tools/dev_finish.py` records development usage and requests normal restart/completion notices.

## Optional local chat adapter

`src/core/local_llm.py`, called by `chat._local_rag`, implements only Ollama-compatible `/api/chat` HTTP. No model, installer or startup logic is bundled. Add future protocols here, not throughout UI/chat code.

No configuration means no probing, service access or fallback offer. Deploy a service separately and set both `MYN_LOCAL_LLM_URL` (complete HTTP/HTTPS endpoint on a loopback IP, e.g. `http://127.0.0.1:11434/api/chat`, IPv6 `[::1]` allowed) and `MYN_LOCAL_LLM_MODEL` (exact already-installed model, no default/download). Reject hostnames, credentials, queries and fragments. Missing/invalid configuration disables it; clear either variable to disable, and restart after persistent environment changes. Disable proxies and redirects.

Only transient Claude failure during read-only memory Q&A may offer fallback, and the current `consent_local` callback must obtain actual consent. Missing credentials, authentication failures, ordinary chat and local commands cannot enable it. Send question, retrieved evidence, time and preferences only; grant no tools/files/SQL. Require answer, citations and grounded fields and apply source/uncertainty checks. Limit responses to 256 KiB and timeout to 70 seconds; no automatic retry or alternate-model fallback.

## Source and data layout

| Location | Responsibility |
| --- | --- |
| `src/myn_gui.py`, `gui_*.py` | Main window, reader, speech, tasks and panels |
| `src/web/`, `webview_*.py`, `web_panels*.py` | WebView2, animations, media/audio and bridge |
| `core/chat.py`, `claude_client.py`, `local_llm.py` | Routing and model connections |
| `conversation_log.py`, `message_log.py`, `storage.py`, `memory_*`, `scene_memory.py` | Originals, facts, evidence, retrieval and scenes |
| `preferences.py`, `preference_history.py`, `personal_data_tools.py` | Preference learning/history and data management |
| `stt*`, `tts*`, `qwen3*`, `worker_process.py`, `interaction.py` | Speech, workers, scenes and interruptions |
| `local_actions.py`, `permission_control.py`, `agent_*`, `self_update.py` | Permissions, tasks and recoverable updates |
| `reporting.py`, `report_sources.py`, `usage_stats.py` | Reports, evidence and usage |
| `src/data_location.py` | Durable paths and legacy migration |
| `personal_data/` | Databases, messages, memory, attachments, indexes, settings, backups and media |
| `data/` | Legacy data; never delete based on its name |
| `tests/`, `tools/dev_finish.py`, `self-update/` | Isolated validation, completion and source backups |

Never commit or copy models, environments, private data, credentials or large media into working copies. Legacy brain-settings.json only supports migration and cannot enable local chat. Old backups do not describe current features.

## Git and GitHub

`D:\MYN` uses Git on `main`. The English source baseline is its first version. `origin` points to the public [showor1d/myn](https://github.com/showor1d/myn), URL `https://github.com/showor1d/myn.git`. Commits and uploads are separate. Review before requested pushes; never expand public tracking to private data. Repository-only identity is `MYN Local Development <myn@localhost.invalid>`; do not change global identity.

Track source, tests, assets, build/requirements/setup scripts, development tools, `.claude/settings.json` and this document. The root `.gitignore` allowlist excludes private data, secrets, recordings, logs, models, runtimes, backups, old installations, third-party programs and generated results. Review new root files before changing it; never force-add exclusions.

Inspect status/diff before work and stage only reviewed changes. No unknown merges, force-pushes, untracked cleanup or destructive resets without explicit authorization. The user's one-time request to replace the earlier baseline with this English first version authorizes only that replacement; future work uses normal commits. Self-updates do not automatically commit/upload. Use Git Credential Manager and never expose tokens in source, URLs, docs or chat. Git preserves committed project files, not personal backups; GitHub contains only pushed commits. Prefer reviewable revert commits when undoing published changes.

## Development and verification

1. Read current rules, inspect callers/paths/concurrent changes, and back up and verify source/tests/docs. Data migrations require a separate verified full backup.
2. Edit a source copy. Check callers, dynamic bindings and compatibility before removing code. Keep help, comments, UI and docs aligned.
3. Isolate all four MYN data-directory variables in temporary folders and remove real API keys/local-model settings. Never test with personal databases or media.
4. Run Python 3.9 `python -B -m unittest discover -s tests -v`; speech checks use `local-tts/.venv/Scripts/python.exe`. UI changes require relevant real Tk/WebView2 smoke checks; check changed script syntax. Complete the full regression before deployment.
5. Local-adapter tests use synthetic evidence and a temporary loopback server to verify opt-in, endpoints, errors, redirects/proxies and rejected citations. Chat tests cover consent, Claude failures and memory preservation. They do not prove real model quality.
6. Before deployment verify original hashes are unchanged by others; deploy dependencies before callers, verify resulting hashes and isolated imports. Source deployment does not imply data edits or installing services/models.
7. Run `C:\Program Files\Python39\python.exe D:\MYN\tools\dev_finish.py` at completion. Preserve the Stop hook; Codex invokes the same tool manually. Request normal idle restart, never forced termination.

Explicitly requested installer work must check manifests, clean install, available historical upgrades, recovery and uninstall. Preserve unknown files/personal data and disclose untested versions.

## Current verification status

The English baseline passed the full Python 3.9 suite: 816 tests, with 37 environment-dependent skips. The speech environment passed 139 tests with no skips. Nine real Tk/WebView2 smoke checks passed: Agents navigation, Expenses/receipts, media, interaction scenes, audio console, calendar, alarm popup, reports and timer HUD. Python syntax, all frontend JavaScript syntax, and the English comments/documentation audit passed. Nine presentation/compatibility tests cover English UI/help, Chinese commands and preserved originals, numeric units, inference markers and wake acknowledgement echoes. Audio checks used synthetic signals; no paid-model or live-microphone recognition quality claim is made. Deployment verifies source hashes and isolated installed imports; publication requires identical local/remote main commits and a clean reviewed working tree.

## Permanent constraints

1. Back up and verify before data changes/migration. Preserve personal data, backups, settings, permissions, media and unknown files. Never expose secrets/passwords/private content. Verify exact ownership and paths before cleanup.
2. Save ordinary messages verbatim first; compatibility commands remain in history but bypass extraction. Facts link to originals; inferences stay separate. State uncertainty without evidence; cite only current valid IDs. Plans/guesses/save times are not completed events.
3. Memory edits, classifications, types, trash, restoration and legacy save require reading, preview, real confirmation and unchanged-state verification, defaulting to cancellation. No unattended changes, arbitrary SQL, bulk/permanent memory deletion. Extraction uses fixed validated writes. Profiles may contain labeled inference; user corrections prevail.
4. Claude remains the main model. Local chat defaults off with no bundled model/service. Only explicit valid configuration, transient Claude failure and current consent permit read-only memory fallback. Never silently select/download a model. Speech models remain independent.
5. Follow existing action grants/confirmation policies. New permissions/policies always need actual confirmation. Chat tools cannot delete files or execute arbitrary commands. Background ordinary commands may run; risky deletion, system/registry/service/power/process operations, remote repository writes/discarded changes, downloaded-code execution, elevation and private-data/credential access require confirmation. File writes follow their rules. Confirm calendar deletion and sensitive scheduled actions at creation.
6. Do not restore retired search/summary/stats/Brain/CMD HUD or removed management pages. Keep existing conversational tools, retrieval, profile, preferences, web, data and backup functions.
7. No EXE build/rebuild/run without explicit request. Routine operation remains the source GUI. Requested packaging must protect upgrades, precise cleanup, recovery and personal data.
8. Update this document in place for every change; remove outdated text instead of appending history/conflicts. Separate isolated, deployed and live verification; detailed history remains elsewhere.
9. MYN self-edits only a program copy with temporary data, no outside paths or risky commands. Passing baseline tests and self_update.check plus an adopted plan/explicit fix request authorize automatic backup/deployment/idle restart. Unadopted plans cannot replace source. Verify loading and roll back unconfirmed startup. MYN cannot edit self_update.PROTECTED files or this section; an explicitly authorized user/developer may. Exclude data/models/environments from copies.
10. Record development usage and run tools/dev_finish.py for normal reopening and English completion notices. Never forcibly kill MYN. Use registered sessions, avoid double-counting cache/reasoning tokens and retain estimate labels/pricing sources.
