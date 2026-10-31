from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import simpleSplit
from pathlib import Path
from xml.sax.saxutils import escape

ROOT=Path('/workspace/scratch/e1be1c49d99a')
OUT=ROOT/'output/pdf/Twin-OnlyFans-Commercial-Readiness-Audit-2026-10-31.pdf'
for name,file in [('DejaVu','DejaVuSans.ttf'),('DejaVu-Bold','DejaVuSans-Bold.ttf')]:
    pdfmetrics.registerFont(TTFont(name,'/usr/share/fonts/truetype/dejavu/'+file))
pdfmetrics.registerFontFamily('DejaVu',normal='DejaVu',bold='DejaVu-Bold',italic='DejaVu',boldItalic='DejaVu-Bold')
NAVY=colors.HexColor('#142B43'); BLUE=colors.HexColor('#2463A5'); GRAY=colors.HexColor('#526273'); LIGHT=colors.HexColor('#EDF3F8'); RED=colors.HexColor('#A33936')
styles=getSampleStyleSheet()
styles.add(ParagraphStyle(name='TitleX',fontName='DejaVu-Bold',fontSize=21,leading=25,textColor=NAVY,spaceAfter=14))
styles.add(ParagraphStyle(name='SubX',fontName='DejaVu-Bold',fontSize=12,leading=15.5,textColor=NAVY,spaceBefore=8,spaceAfter=5,keepWithNext=True))
styles.add(ParagraphStyle(name='BodyX',fontName='DejaVu',fontSize=9.5,leading=13,textColor=NAVY,spaceAfter=6))
styles.add(ParagraphStyle(name='SmallX',fontName='DejaVu',fontSize=8.2,leading=11.3,textColor=GRAY,spaceAfter=6))
styles.add(ParagraphStyle(name='CellX',fontName='DejaVu',fontSize=8.4,leading=11.2,textColor=NAVY,spaceAfter=0))
styles.add(ParagraphStyle(name='HeadX',fontName='DejaVu-Bold',fontSize=8.4,leading=11.2,textColor=colors.white,spaceAfter=0))
styles.add(ParagraphStyle(name='BulletX',fontName='DejaVu',fontSize=9.5,leading=13,textColor=NAVY,leftIndent=10,firstLineIndent=-10,spaceAfter=6))
story=[]
def p(s,style='BodyX'):story.append(Paragraph(s,styles[style]))
def h(s):p(s,'SubX')
def bullet(s):p('• '+s,'BulletX')
def title(n,s,sub):
    if n>1:story.append(PageBreak())
    p(f'{n:02d} / {sub.upper()}','SmallX');p(s,'TitleX')
def table(headers,rows,widths):
    data=[[Paragraph(escape(x),styles['HeadX']) for x in headers]]
    data += [[Paragraph(x,styles['CellX']) for x in row] for row in rows]
    t=Table(data,colWidths=widths,repeatRows=1,hAlign='LEFT')
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),NAVY),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,LIGHT]),('LINEBELOW',(0,-1),(-1,-1),.5,colors.HexColor('#CFD9E2'))]))
    story.append(t);story.append(Spacer(1,7))
def link(label,url):return f'<link href="{escape(url)}" color="#2463A5">{escape(label)}</link>'

title(1,'OnlyFans CRM\nCommercial readiness','Developer call brief')
p('31 October 2026 · Brisbane time · Read-only production and source audit','SmallX')
p('<b>Decision: not ready to promise the requested capacity.</b> The system can send replies and paid media today, but the running account-wide send gate mathematically prevents 10,000 chatting fans per creator per day. A larger server alone cannot remove that limit.')
h('Open the call with this')
p('“We need to support creators with 100,000 fans and around 10,000 chatting each day. Different fans must be handled concurrently, with an intentional delay of at least 25 seconds and a response within three minutes. We also need reliable paid-media delivery, purchase recognition and human takeover. Please explain the current bottlenecks, which are inherited from FASTT, which are our additions, and prove the supported throughput on the actual production build.”')
h('The seven decisions to get from the developer')
for s in [
'<b>Account throughput:</b> why is there a hard 20-second gap between sends across an entire creator account, and what provider-tested limit can replace it?',
'<b>Parallel work:</b> how will we move beyond four preparation tasks per account and the default 64-account supervisor limit?',
'<b>Timing:</b> which timestamp starts the 25-180 second clock, and how are delayed, skipped and unanswered turns measured?',
'<b>Content sales:</b> can one controlled scenario prove conversation assessment, correct media and price, provider receipt, payment, unlock and purchase memory?',
'<b>Imports:</b> how will new messages, purchases, history and vault imports all progress without starving one another?',
'<b>Operator control:</b> can a chatter see exactly who AI owns, interrupt a pending send, and hand back safely? What is actually enforced when Infloww is also used?',
'<b>Release ownership:</b> who owns one reproducible source release, its regression checks, monitoring and rollback?'
]:bullet(s)
h('A concrete example that exposes the problem')
p('Twenty different fans message the same creator at once. At the current 20-second account-wide gap, the twentieth first reply cannot start until at least 380 seconds after the first send. If the first reply waits 25 seconds, the twentieth is no earlier than 405 seconds, or 6 minutes 45 seconds. That already fails the three-minute requirement, without needing a 100,000-fan test.')
p('Audit scope: inspected live AWS containers, resource settings, runtime source, database aggregates, recent logs, stored test reports, relevant GitHub changes and upstream FASTT source. No settings were changed, no services restarted, and no fan messages or purchases were sent. This is an evidence-based diagnosis, not load or security certification.','SmallX')

title(2,'Define the capacity correctly','Workload and architecture')
p('Assumption for planning: 100,000 stored fans and 10,000 distinct chatting fans <b>per creator per day</b>. Daily active fans are not the same as simultaneous requests. Reply turns, message bubbles, media sends and peak arrivals must all be specified.')
table(['AI replies per active fan/day','Replies per creator/day','Average replies/second','100 creators: replies/day'],[
['1','10,000','0.116','1 million'],['5','50,000','0.579','5 million'],['10','100,000','1.157','10 million'],['20','200,000','2.315','20 million']], [145,115,100,139])
p('<b>Current theoretical ceiling:</b> one send every 20 seconds = 3/minute = 180/hour = 4,320/day per creator. This is an upper bound, not an achieved rate. Multiple bubbles, media cards, manual sends, failures and other overhead can reduce useful reply capacity further.')
p('At 10 replies per active fan, the average demand is about <b>23 times</b> that ceiling. A planning burst of five times the daily average is 5.79 reply turns/second per creator, or 579/second across 100 creators. The burst factor is a proposed test assumption, not measured customer behaviour.')
h('What the running code does')
bullet('<b>Generation:</b> FASTT_PREPARE_CONCURRENCY defaults to 4 per account and is clamped to 16. Logs confirm four in-flight tasks. Raising it alone does not remove the send gate.')
bullet('<b>Sending:</b> native_pilot/tuning.py enforces a 20-second floor. native_pilot/send_gate.py serializes the wire path per account. The database shares next_dispatch_at with other sends.')
bullet('<b>Accounts:</b> the supervisor defaults to 64 eligible accounts, with a configurable ceiling of 256. A default deployment cannot serve a 100-creator fleet without changes and resource validation.')
bullet('<b>Budgets:</b> the current AI sender uses a rolling 2,000-per-fan daily allowance. Do not confuse legacy account limit_units fields with the effective AI limit. Model cost caps are a separate constraint.')
h('Translate “no queues” into an enforceable requirement')
p('Use concurrent work across independent fans, ordered work within each fan, and no sustained backlog inside the agreed load envelope. A durable internal scheduler is still necessary for the 25-second delay, restart recovery and duplicate protection. It must not make one fan wait for another fan’s full conversation.')
p('Retain creator-wide provider rate limits, but base them on verified throughput and provider behaviour. No evidence found establishes the hard-coded 20-second rule as an OnlyFans-mandated limit. Do not simply remove it and assume unlimited sends are safe.')
p('Implementation options include a durable job store with short claims or a message broker partitioned by tenant + creator + fan. AWS documents parallel FIFO processing across message groups; PostgreSQL documents SKIP LOCKED for queue-like consumers. Neither mechanism bypasses OnlyFans or model-provider limits.','SmallX')

title(3,'What is actually running','Production observations')
table(['Area','Observed state'],[
['Host','m7i.2xlarge, 8 vCPU and about 32 GiB RAM. The older t3.large sizing is obsolete.'],
['Services','One API container, one sync worker, one sender supervisor and a local PostgreSQL database share this host with dashboards and other services.'],
['Container limits','API: 0.5 CPU / 256 MiB / one Uvicorn worker. Sync worker: 1 CPU / 768 MiB. Sender: 2 CPU / 2 GiB. Database: 2 CPU / 4 GiB, 150 connections.'],
['Current activation','Maile, Alva and Naomi have enabled runtimes. Emma, Amelia and Cristina are connected but runtime-disabled. Katie and Nicky are paused/disconnected. This is not evidence of an activation bug.'],
['Existing storage','Database estimates: about 118,000 fan rows, 52,000 chat rows and 967,000 messages across accounts. Stored rows do not demonstrate simultaneous chat capacity.'],
['Host headroom','Snapshot showed about 26 GiB available memory and 45% root-disk use. Database CPU was about 1.55 cores of its 2-core container limit. A single sample is not a saturation study.']],[110,389])
h('Completed turns since the current sender started')
p('Window: 31 October, 07:20 AEST through approximately 09:44 AEST. First confirmed receipt per non-follow-up input, measured from that input’s selected provider-message timestamp. Completed turns only; this excludes unanswered work and is not a service-level pass rate.','SmallX')
table(['Creator','Turns','Median','95th percentile','Longest'],[
['Maile','26','52.8 sec','81.8 sec','111.0 sec'],['Alva','21','64.8 sec','89.8 sec','232.2 sec']],[104,58,102,125,110])
p('<b>The three-minute maximum is not yet demonstrated:</b> Alva had a completed turn above 180 seconds even in this small post-release sample. Over the broader 24-hour window, 70 Maile and 63 Alva turns had confirmed first receipts; the numbers are far below the proposed workload.')
p('<b>Timing needs one definition.</b> Two recent replies were less than 25 seconds after a later bubble in a grouped input, but at least 25 seconds after the earliest input. They are not proven violations of a first-message timer. Measuring from the earliest grouped message also exposes older history/backlog, including very old Maile inputs. Report new live turns, backlog recovery and deliberate holds separately; do not silently discard any of them.')
h('The existing load evidence is too small')
p('The corrected stored synthetic burst test exercised five accounts with ten fans each, 50 replies total. At a 20-second throttle, one account approached 180 seconds. A five-second test looked faster, but used simulated model/provider responses and does not prove the current sender permits that interval or that OnlyFans supports it. Initial failed harness runs were superseded by corrected reports; neither set is a 10,000-fan capacity result.')

title(4,'Which layer caused which issue?','Bug attribution')
p('“FASTT” is used for both the original engine and Twin’s modified runner. Attribution below names the layer supported by source or repair history. A repaired symptom is kept as a regression requirement, not presented as an unfixed bug.')
table(['Finding','Origin and evidence','Current status / developer action'],[
['Unsolicited photo ratings and point deductions','<b>Inherited FASTT.</b> Upstream ai_chatter.py explicitly requests ratings and deductions for ordinary photos.','<b>Repair present.</b> Running source requires an explicit rating request and prioritizes message/caption. Its hash matches the latest photo/caption release. Preserve this in the consolidated release.'],
['Photo or media receives no usable response','<b>Mixed.</b> Vision is inherited; admission and source validation are in Twin’s native_pilot adapter.','Caption fallback is deployed. Recent logs still contain unsupported-media and unsupported-paid input events. Define a useful fallback or visible handoff for every supported type, without inventing image contents.'],
['Manual free/paid media blocked by old history','<b>Twin integration.</b> Archive/commerce admission guards and API/sender drift were repaired in PRs 62/64.','<b>Recent evidence is positive:</b> 35 accepted manual intents matched stored provider message text and price after trimming whitespace. This does not reconstruct an unsaved UI draft or test every attachment path.'],
['Replies blocked when saved creator facts age','<b>Twin integration.</b> A 120-second source freshness rule blocked unchanged facts; PR 67 changes validity to explicit edits.','Reported deployed repair. Keep missing-source, binding and explicit-change checks; include in regression testing.'],
['Old identity question pauses a resumed chat','<b>Twin replay/control adapter.</b> Completed input members were retriggered. PR 63 filters completed triggers.','Repair exists. Test new identity questions separately from historical questions after human hand-back.'],
['Narrow keyword gate prevents proactive offers','<b>Twin contextual feature.</b> PR 56 added contextual_offers; PR 68 replaces the keyword gate with conversation assessment.','New evaluator is in live source. Successful delivery from this new proactive path remains unproven by this audit.'],
['Account-wide 20-second sends / four preparation slots','<b>Twin production runner policy.</b> Directly observed in tuning.py, send_gate.py and service.py.','<b>Current scale blocker.</b> Establish a proven provider budget and redesign concurrency around it. It is not established as an unavoidable FASTT or OnlyFans limit.'],
['Purchase and vault imports defer/starve','<b>Twin sync integration plus provider dependencies.</b> PR 60 fixes fairness/date parameters; PR 69 adds a bounded Emma import turn.','Imports are progressing, but history scans remain incomplete and freshness still occasionally blocks contextual sales. Generalize fairness and measure age per lane.']],[113,181,205])
p('Provider throttling, session expiry, model outages and proxy failures require their own category. They must be distinguished from application defects by request, timing and receipt evidence. The audit does not establish an exact safe OnlyFans request rate or every historical regression’s first introducing commit.','SmallX')

title(5,'Make media sales and onboarding dependable','Content, purchases and memory')
h('There is some paid delivery, but the entire sales loop is not proven')
p('In the 24-hour receipt-matched subset, Maile had 109 sent messages including 3 paid media cards; Alva had 93 messages including 1 paid card. No purchased marker was present on those four cards in the inspected message rows. This is <b>not</b> a conversion-rate estimate or proof that no purchases occurred elsewhere.')
p('The new conversation evaluator is enabled and running. In a bounded two-hour log sample, 38 evaluations ended in the combined reason <b>no_supported_interest_or_evaluation_failed</b>; two further opportunities were withheld for stale purchase state. No recent row was found in of_contextual_offer. These observations do not prove that every rejected conversation should have received an offer, but they do mean we cannot yet claim successful proactive selling from this path.')
bullet('Separate a legitimate “keep chatting” decision from provider errors, parsing failures, missing evidence, unmatched content and stale payment data. The current combined reason hides the distinction.')
bullet('Prove one controlled offer from engagement to matching content, correct price/caption, delivery receipt, payment, unlock, owned-media exclusion and subsequent follow-up.')
h('Description coverage remains incomplete')
p('Counts below mean a nonempty description or video_description on an active vault item. They do not prove accuracy or approval, and some metadata may exist elsewhere.','SmallX')
table(['Creator','Photos described / stored','Videos described / stored'],[
['Maile','367 / 524','4 / 26'],['Alva','1,138 / 1,769','17 / 812'],['Naomi','1,895 / 3,020','32 / 770'],['Emma','0 / 420','0 / 16']],[110,194,195])
bullet('Import every eligible folder/page, verify media ownership and playable assets, describe content, attach approved prices and boundaries, and measure sale-ready inventory. Import count alone is not readiness.')
bullet('Preserve exact manually selected attachments, previews, caption and price. If the user requests a different item, evaluate it independently; do not force purchase of the earlier card. Reuse an existing card for the same item and stop chasing ignored offers.')
bullet('Do not announce that content has been sent until there is a confirmed provider result. On an uncertain response, reconcile before any retry that might duplicate the paid offer.')
h('Onboarding needs explicit completion gates')
p('Emma’s vault is progressing, but every inspected launch-history scan still showed needed, with no completion timestamp. Amelia and Cristina had not completed purchase scans. Show separate progress and retryable reasons for connection, fans, history, vault, descriptions, purchases and activation.')
p('History-memory is active. A separate profile-learning pipeline is overdue; consolidate ownership and expose freshness. Preserve the intentional new-fans-off rule.')

title(6,'Agency operations must be safe and observable','Controls and reliability')
h('One effective owner per conversation')
bullet('Display AI active, waiting to send, human controlled, paused and failed with concrete reasons and next actions. An “automated” flag alone does not show that a reply can currently be sent.')
bullet('Human takeover must cancel queued or generating AI work, check authority immediately before transport, and preserve any send already accepted by the provider. Hand-back must not replay old questions or duplicate pending cards.')
bullet('Native OnlyFans ownership-list syncing is a single-fan Maile pilot in PR 70. Its list membership cannot prevent a chatter sending in Infloww. Verify external refresh, routing, handover delay and the policy for conflicting sends before promising coexistence.')
bullet('Bulk enable/disable must be resumable, scoped, revision-checked and explicit about skipped fans. Existing chats and newly arriving fans are different populations; honour the current new-fans-off rule.')
h('Handle failures without silence or duplicate sends')
p('The live work board contained one Maile unresolved-delivery hold, frozen-dispatch-unavailable. A two-hour sample logged that reconciliation reason 226 times. It also logged 1,146 reply-schedule-terminal holds and repeated hold/resume transitions. These are event counts, not distinct fan failures. Trace the affected durable states, stop unproductive retries, and make unresolved outcomes actionable.')
bullet('Every eligible turn needs a durable outcome: sent with provider ID, explicitly skipped, waiting with deadline, human-owned, or failed with recovery. An empty live queue is not proof that every input was answered.')
bullet('Retry model and network failures with bounded backoff and budgets. A timeout after a provider accepted a card must not trigger a second sale. Use one-use intents, receipt reconciliation and per-fan locking.')
bullet('Instrument provider arrival, ingestion, model start/end, eligibility time, wire start and receipt. Track earliest unanswered age, deadline misses, lost inputs, duplicates, offer funnel, stale purchases and recovery time per creator.')
h('Scale the agency-facing system too')
bullet('Use server-side pagination and indexed search for 100,000-fan rosters. Test bulk actions, mobile composer, attachments, price, captions and refresh at that size.')
bullet('Prove tenant/agency isolation across API routes, jobs, media URLs, caches, exports and staff permissions. Verify that a staff member cannot read or operate another agency’s creator. No cross-tenant breach is asserted by this audit.')
bullet('Separate model/provider limits, live reply resources, media analysis and bulk imports. Test global token budgets and fair sharing so one large creator cannot consume the fleet.')
bullet('Treat host loss, database restore and service restart as release tests. Current core services share one host; failover, off-host restore and recovery objectives were not demonstrated here.')
p('<b>Release blocker:</b> production overlays must become one reviewed, reproducible release. Test exact assembled images and preserve newer fixes during rollback. Existing fixture failures are not a green acceptance result.')

title(7,'What must pass before selling this capacity','Developer acceptance plan')
p('<b>Proposed baseline:</b> 100,000 stored fans per creator, 10,000 daily chatting fans, 10 reply turns per active fan, measured with a five-times-average peak. Confirm the desired creator count, active hours and extra message bubbles before committing to a commercial limit.')
table(['Test','Required evidence / pass condition'],[
['Full-size dataset','1, 10 and 100 creators; up to 10 million fan records at fleet scale. Inbox, filters, bulk controls and imports remain responsive. Suggested UI target: p95 below 2 seconds for paginated inbox/search.'],
['Concurrent live-turn simulation','100,000 reply turns/day per creator, then five-times-average peaks. Use realistic model latency, token sizes, media preparation and provider responses. Test the actual assembled API, worker, sender and database.'],
['Burst example','1,000 fans message one creator in the same minute while history and vault imports run. All eligible first replies must meet the 180-second deadline, with no duplicates or unaccounted inputs.'],
['Timing','Agree first unanswered message / grouped input clock. Enforce no earlier than 25 seconds from the agreed anchor. Every in-scope response must be within 180 seconds in acceptance; measure p50, p95, p99, max and unanswered. Aim below the ceiling to leave headroom.'],
['Isolation and takeover','A slow creator, human takeover, paused fan or changed account session must not stall other creators or leak content. Zero unauthorized or post-takeover sends in the test.'],
['Sales lifecycle','Explicit request and proactive interest both tested; changed request, ignored offer, already-owned media, insufficient catalogue, manual caption/price, purchase/unlock and follow-up all verified.'],
['Faults and recovery','Inject model timeout, invalid JSON, 429, expired session, proxy failure, lost receipt, duplicate/out-of-order event and crash during send. Verify durable recovery without duplicate charges or false sent status.'],
['Duration and rollout','24-hour mixed-load soak including a deployment and restart. Then a controlled provider-backed canary to verify actual connection limits. Expand in measured cohorts with rollback and monitoring.']],[117,382])
h('Priority and ownership')
bullet('<b>P0, backend/runtime owner:</b> reconcile the exact live source, settle the provider throughput budget, remove the architectural account bottleneck through a tested design, and prove the 25-180 second target.')
bullet('<b>P0, commerce/integration owner:</b> prove contextual paid delivery, purchase/unlock reconciliation, usable media coverage and trustworthy manual sends; resolve uncertain deliveries.')
bullet('<b>P1, platform/data owner:</b> fair imports, memory freshness, database/pool sizing, isolation, monitoring, recovery and one repeatable release process.')
bullet('<b>P1, frontend/agency operations owner:</b> actionable statuses, dependable takeover, large-roster performance, bulk progress and completed onboarding.')
p('Define the supported load and alert on provider outages, disconnected accounts and human pauses. Extra servers cannot guarantee provider availability. Use synthetic traffic for stress tests, not real fans.','SmallX')

title(8,'Evidence and limits','Technical handoff')
h('Pinned production observations')
p('AWS instance i-09a9dd3bd749643a5, ap-southeast-2. Observations began at 31 October 2026, 09:36 AEST. Core container restart counts were zero at inspection. Repository references describe source/repair evidence; deployment assertions below come from direct running-container inspection.','SmallX')
table(['Running component','Image identifier'],[
['API','a16a183e08ff53b4e45be76df9be61ee82515abf11378bd8903f14f130a2f228'],
['Sync worker','4b66e8cd5138283a52dda551733ec94bb571e396a0fb9ce6d6eca54cf8985beb'],
['Sender supervisor','da497c8ee087ea709d27cf53ab740b290ee270ba05baae5eb940acaea8d6248b'],
['Twin / OnlyFans dashboards','34dd3c4995040e7906506f8502618ac8e9f9ba6cb515a5f158576d29fcffb382']],[128,371])
p('Sender files ai_chatter.py, native_pilot/runner.py and native_pilot/ledger.py matched the SHA-256 hashes of the latest caption-fallback release artifacts. Source paths: service/native_pilot/{tuning,send_gate,service,supervisor,pacing}.py; service/onlyfans_runtime/provider_read_budget.py; service/automations/{contextual_offers,content_prompts}.py.','SmallX')
h('Source and repair references')
refs=[
('FASTT upstream photo routing / prompt','https://github.com/BlackPhoenixSlo/inflowwkiller/blob/main/service/automations/ai_chatter.py'),
('Twin backend PR 60: purchase-read scheduling and date filters','https://github.com/twinagency/twin-onlyfans-backend/pull/60'),
('PR 63: old identity question replay','https://github.com/twinagency/twin-onlyfans-backend/pull/63'),
('PR 64: manually priced media history holds','https://github.com/twinagency/twin-onlyfans-backend/pull/64'),
('PR 65 / dashboard 340: bulk AI controls','https://github.com/twinagency/twin-onlyfans-backend/pull/65'),
('PR 67: persistent creator facts','https://github.com/twinagency/twin-onlyfans-backend/pull/67'),
('PR 68: conversation offers and new creator defaults','https://github.com/twinagency/twin-onlyfans-backend/pull/68'),
('PR 69: Emma import fairness','https://github.com/twinagency/twin-onlyfans-backend/pull/69'),
('PR 70: native ownership-list pilot','https://github.com/twinagency/twin-onlyfans-backend/pull/70'),
('AWS: parallel FIFO message groups','https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/FIFO-queues-understanding-logic.html'),
('PostgreSQL: SELECT and SKIP LOCKED','https://www.postgresql.org/docs/current/sql-select.html')]
for label,url in refs:p(link(label,url),'SmallX')
h('Reproduce or extend the audit')
p('Read-only SSM records include ff8d623e-52d4-429f-a9f1-5cf6fc426601 (host), 078756fa-5201-437f-861f-9d103798e0d4 (configuration), f38f8ce5-f7cd-4e48-8ae5-20a2f4de0432 (receipts/media), d0d5c9bc-473c-4fff-9a93-c2ed55f4e1bd (post-release samples), and c5d31e62-5acb-4adc-a6bc-a5b50883c6d7 (source hashes/logs). Queries used read-only transactions and bounded statement timeouts.','SmallX')
p('Stored capacity reports: /var/lib/twin-release/fastt-capacity-20261029T140216Z/verified-harness-20261029T143129Z/. Latest inspected photo/caption release: /var/lib/twin-release/caption-fallback-20261031/. Existing runner and vision reports passed 262 and 74 checks; a service-suite pace-stop fixture timeout remains in the stored reports, also recorded on prior baselines. These are inspected historical test results, not tests newly run during this audit.','SmallX')
p('<b>Not established:</b> safe sustained OnlyFans send/read rate at the proposed load; a 100-creator full-stack load pass; a completed proactive-sale purchase/unlock canary; exhaustive UI/security/restore coverage; or a first-introducing-commit attribution for every historical issue. No full rebuild, production load test, live send or purchase test was performed.','SmallX')

def footer(c,doc):
    c.saveState(); w,h=doc.pagesize
    c.setStrokeColor(colors.HexColor('#DCE4EC'));c.line(48,42,w-48,42)
    c.setFont('DejaVu-Bold',8);c.setFillColor(NAVY);c.drawString(48,h-29,'TWIN TECHNOLOGIES')
    c.setFont('DejaVu',8);c.setFillColor(GRAY);c.drawString(48,28,'OnlyFans CRM · Commercial readiness · 31 Oct 2026')
    c.drawRightString(w-48,28,str(doc.page));c.restoreState()
doc=SimpleDocTemplate(str(OUT),pagesize=(595.28,841.89),rightMargin=48,leftMargin=48,topMargin=53,bottomMargin=55,title='Twin OnlyFans CRM Commercial Readiness Audit',author='Twin Technologies',pageCompression=1)
doc.build(story,onFirstPage=footer,onLaterPages=footer)
print(OUT)