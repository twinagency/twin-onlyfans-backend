# Moxy: complete saved-conversation review

Completed 2 November 2026. Scope: Amelia and Cristina, for the new Moxy reproduction. This supersedes the earlier 18-conversation close-reading limit. It does not describe a new implementation or a production deployment.

**The saved export is now fully covered: 208,334 messages across 30,526 account/fan threads. The larger review confirms active selling, but shows that its rhythm comes from several different activities: promotional distribution, invitations, product sequences, purchase-related continuation, and ongoing customer relationships. A single message-count rule would not reproduce that workflow.**

The additional reading also found explicit paid-chat commitments, requested card resends, purchases consistent with returning to older offers, free replacement media, unresolved bonus obligations, and historical messages that conflict with Moxy's current instructions. These distinctions materially change how the examples should be used.

## 1. What was actually reviewed

| Coverage | Amelia | Cristina | Total |
|---|---:|---:|---:|
| Saved message records | 111,401 | 96,933 | 208,334 |
| Account/fan threads with messages | 13,290 | 17,236 | 30,526 |
| Two-way threads, read in captured chronological order | 741 | 1,473 | 2,214 |
| Messages in those two-way threads | 17,613 | 37,299 | 54,912 |
| Remaining one-way threads | 12,549 | 15,763 | 28,312 |
| Messages in those one-way threads | 93,788 | 59,634 | 153,422 |

There are also 29,253 transaction rows and 83,128 fan records. Every row was processed for coverage, joins and data-quality checks. Transactions within each two-way thread's captured time span were presented alongside the conversation; earlier/later transaction counts were retained separately.

The semantic review used 63 two-way batches, three repeated-text dictionaries, and 18 one-way batches. Repeated message bodies were referenced to the dictionary rather than silently removed. The one-way histories were grouped into 2,719 distinct ordered sequences using creator, normalized text, direction, price, attachment count and automation metadata. Every distinct sequence and new body was reviewed; every member thread remains in the coverage ledger. Original IDs and timestamps remain in the source index and unchanged raw export. This is full saved-record coverage through deduplication, not a claim to have separately close-read every identical campaign copy or every one-way timestamp.

Of the one-way threads, 28,303 contain only outbound messages and nine contain only incoming messages. The latter include incoming priced media, which is not a creator sale. A one-way export does not establish that there was never a conversation outside the captured records.

`coverage_verification.json` reconciles all 30,526 thread keys and 208,334 message records against the unchanged sources. It also verifies that each grouped sequence has matching normalized content and metadata. No source threads remain pending. Coverage verification does not establish that every interpretation is error-free or that the provider's complete history was exported.

No new provider imports, OpenRouter calls, application edits, production changes or customer messages were made during this review.

## 2. Findings that the small initial sample did not adequately establish

### A. High visible sales volume is a combination of promotional distribution and individual selling

The export contains 122,443 priced outgoing messages with attachments and 21,450 zero-price outgoing media messages. These are sends, not purchases or unique products.

Many outbound histories share entire ordered caption sequences. For example, Cristina's one-way groups 1991–1997 alone show large populations receiving the same two paid-caption sequence, sometimes with a short follow-up. There are extensive welcome offers, seasonal promotions, free-image hooks, online-reactivation messages and direct invitations. The two-way threads then contain individualized questions, product discussions and progression between additional cards.

There is also direct textual evidence of campaign interleaving: Cristina fan 597273800 says an item was already seen; the reply calls it a mass message. Later the account warns that promotional items may overlap previously acquired content. Source messages: `11867389308136`, `11867428670181`, `11877876817220`.

**Implication:** reproduce the agency's existing promotional and conversational workflows with shared ownership and purchase state. Do not interpret every promotional card as the next conversational step. All `mass_run_id` fields and outgoing `sender_name` fields are blank, so exact provenance and campaign targeting still need their existing operational records. Repeated text alone is not proof of a broadcast or human authorship.

### B. Invitations and follow-on offers are explicit, rather than passive waiting for a purchase question

The histories contain specific invitations, preference choices, readiness acknowledgements, free previews and follow-on prompts embedded in paid captions. Examples include a four-door sequence leading toward VIP (one-way group 1703), a low-price vault bundle asking the recipient to reply with a selection request (group 2610), and a caption promising extra pictures plus a discounted follow-on bundle (group 2704).

These messages are evidence of active commercial direction. They are also commitments: a promised extra or next step needs an actual product and fulfilment path. A generic model reaction after a fan accepts would lose the workflow.

**Implication:** retain the documented sequence and the specific invitation awaiting a response. An acceptance must be interpreted in that context. It leads to the relevant authorised offer or next action, not an automatic charge. The histories do not authorise inventing a new item, reward or discount.

### C. Purchase-related continuation can happen without another fan message

Cristina fan 599763924 receives an 8,500-cent card at 15:53:00 on 23 October, one intervening outgoing text, and an 11,000-cent card at 15:55:46. A same-fan 8,500-cent PPV transaction marked cleared occurs between them. Card IDs: `11760678280325`, `11760701716906`.

Other captured sequences also place the next card without a new incoming text. Some explicitly ask the fan to continue unlocking while occupied. This shows why waiting for two new fan replies after every purchase could diverge from the observed process.

**Limit:** the transaction lacks a card ID. This is a plausible purchase-related sequence, not a verified card-to-payment attribution or proof that the next send was actually triggered by that payment.

**Implication:** verified purchase events and approved sequence state must be available to the controller, alongside incoming messages. No global reply-count reset should replace that state.

### D. Returning fans can resume an older sequence days later

Cristina fan 600547961 has an active sequence on 27 October, including a 9,000-cent card labelled as a later vault step (`11818417101364`). On 1 November the ledger contains several same-fan purchases, including a 9,000-cent PPV record, and the fan asks for the next vault prompt (`11886878279703`). The thread has 71 messages and 22 transaction rows.

Cristina fan 596683023 has 537 messages and 58 transaction rows: payment difficulties, ordinary conversation, older purchases, resumed offers, and a complaint about what was still missing after apparent VIP completion. A new session boundary cannot safely erase those obligations.

**Implication:** progression, prior purchases, outstanding promises and rejected items must persist across days and returning visits. Matching a price and time is insufficient to mark a particular step paid; the missing provider linkage must supply that certainty.

### E. Long conversations sometimes fulfil a paid service

Cristina fan 206216912 has 396 captured messages. The account agrees that a final purchase will be followed by another hour to hour-and-a-half of interaction and no further card requirement in that arrangement. The relevant commitment is visible in messages `11877139190139` through `11877174742462`; a substantial conversation follows. The surrounding ledger contains same-fan PPV records, but exact card joins are absent.

**Implication:** a long run without another paid card can be promised service fulfilment. It must not automatically become a sales-drift failure. This example establishes that such commitments occur; it does not approve every aspect of the historical conversation or establish a general paid-chat product absent authorisation.

### F. Repeated paid cards can be resends, replacement choices or revised offers

Cristina fan 576755113 asks to see the same bundle again. The account resends a 5,900-cent, 22-attachment card, declines a request to reduce it to 5,000 cents, and offers an extra instead. Relevant records: original card `11862942999450`, resend request `11863181051019`, resent card `11863188749646`. This is not evidence of two distinct products or two purchases.

Cristina fan 67222007 complains that a bundle was already sent (`11876529967433`). Later records include a different priced card and zero-price replacement media. Cristina fan 88152467 states a 3,600-cent budget (`11891179360112`) and receives a 3,600-cent, 21-attachment bundle (`11891257478138`), different in visible count and caption from an earlier 7,500-cent offer. Without asset IDs, it cannot be classified as a discount on the identical product.

**Implication:** distinguish new offer, requested resend, authorised repricing, alternative product and correction. Counting all of these as equivalent sales opportunities or conversions would distort both behaviour and reporting.

### G. Captioning is part of selling and fulfilment, not merely a photo description

The messages supply real historical paid-card captions. They vary from short labels to longer sales text and may include a sequence name, claimed format/duration, price comparison, incentive, instruction to reply, or promise of additional content.

The full review adds a concrete internal-text leak: five Cristina cards on 16 April carry catalogue-style wording including “send to VIP only for 150$”, while each card's exported price is 5,000 cents with 11 attachments. One source is fan 11771344, message `9279844167358`. The five records are preserved in `internal_caption_mismatches.csv`. Their automation attribution is blank; this is not assigned to the AI. The mismatch is visible in the text and price. Whether the recipients were VIPs, and what the assets actually contained, remain unknown.

Historical captions also contain scarcity, deletion deadlines, large discounts and media-content claims. Their appearance in a transcript does not prove those claims were accurate or that they are currently authorised.

**Implication:** keep separate (1) verified asset facts, (2) the applicable Moxy-approved product/sequence and offer terms, and (3) conversational caption wording. Existing captions can provide examples or reusable copy only after those checks. This review does not identify or require a particular vision or captioning model. No actual media was available to inspect in this export.

### H. Unresolved buying questions and promises matter more than surface fluency

Cristina fan 601716653 asks for a specific clothing format and full-face image, then identifies a mismatch and repeatedly chases promised extras. Source messages include `11869397412420`, `11869436135814`, `11869819060956`, `11884100319606`. Additional selling continues while the obligation remains visibly unresolved. This thread has 117 messages; only two are AI-labelled, so individual failures must retain their actual attribution.

Other threads contain repeated requests for total VIP cost, uncertainty about later levels, rejected items offered again, and custom requests without an authorised quote or confirmed owner. These are failures to answer or complete a transaction, even if the language sounds natural.

**Implication:** preserve the unanswered question and outstanding obligation. Moxy's product-answer and complaint instructions already require this; no new selling strategy is needed.

### I. Ordinary chat and fan exceptions are genuine parts of the operating model

Amelia's sheet explicitly says not to push Yale for sales. In fan 13062832's 169-message thread, he asks to wait until he is ready for content (`11861717210181`); the account accepts (`11861758626042`). There is substantial ordinary work and training conversation and earlier transaction history.

Cristina fan 297171140 has 234 captured messages and 201 transaction rows, with ordinary conversation, tips and operational labels embedded in transaction descriptions. Fan 579961090 has 434 messages and expresses a preference for gifts and conversation rather than repeatedly buying more media.

**Implication:** replacing these relationships with a universal content quota would not faithfully reproduce the documents or the observed activity. The creator-specific no-push, clock, existing-ownership and preference notes need explicit current state.

### J. The history contains both useful execution and evidence of deviations

Examples include pressure after financial difficulty, guilt over silence, unsupported claims, product complaints, privacy/identity contradictions, and prohibited or consent-related material. Some occur before the AI-tagged October exchanges. A January content complaint is visible for Cristina fan 7396096; the April internal-caption leak is another clear historical example.

Conversely, Cristina fans 94476142 and 97454481 receive straightforward answers that a requested category is unavailable; the latter explicitly appreciates the honesty. Useful reference behaviour includes saying what is actually available and maintaining the conversation.

**Implication:** the reproduction needs a discrepancy register. The current PDFs govern; historical deviations must not silently become new policy or training targets. Unknown sender attribution must stay unknown.

## 3. What the full review establishes about timing

There is commercial movement in the first few exchanges in many eligible examples. There are also purchase-related sequences, returning customers, no-push fans, paid interaction, budgeting, complaints, missing content and administrative pauses. Their message intervals have different meanings.

The earlier exploratory computation on this same export remains useful as a descriptive comparison:

| Measure | Amelia | Cristina |
|---|---:|---:|
| Captured segments with a first qualifying paid-media offer | 114 | 242 |
| Median preceding outgoing bubbles | 3 | 4 |
| Middle half | 2–5.75 | 2–8.75 |
| Qualifying repeat-offer intervals | 14 | 93 |
| Median intervening outgoing bubbles | 4 | 5 |
| Middle half | 2.25–6.5 | 3–9 |

These are not newly optimised settings. The computation excludes AI-labelled and unsent messages, known/suspected test fans, and outgoing bodies used with at least ten fans; it separates segments at gaps over six hours and starts counting at captured incoming text. Paid cards themselves are excluded from the respective bubble counts; intervening zero-price media is included. The repeated-body filter also removes some legitimate scripts. The author and product type remain uncertain, incoming promotional traffic is not fully classified, and the first captured offer need not be the first-ever offer.

The full semantic review shows why these intervals must not become one mandatory window: some contain resends, service fulfilment, repair or a returning sequence. The small repeat samples are not a proven revenue-maximising cadence. The current export provides no defensible guarantee that copying its aggregate median will copy earnings.

The documents do include narrow timing instructions: Amelia's talker label allows replies once an hour while not spending (sheet p6); Cristina's clock-labelled fans must not receive replies during the GY shift (sheet p1), and another label permits longer unread periods (p5). Their exact application requires current labels and the existing shift definition. These are not general offer intervals.

For the reproduction, the behaviour to preserve is the applicable invitation, product sequence, purchase/fulfilment state and fan exception. Measure how the resulting comparable conversations differ from approved historical examples; surface unresolved requests and missing next-step data. A timer must not manufacture interest, products, authorisation or a paid send. No new fixed first/repeat window is adopted by this report.

## 4. Mapping back to the governing documents

| Behaviour or constraint | Supplied source | What the export adds |
|---|---|---|
| Amelia's sequence prices, Locker Rooms and VIP restrictions | Amelia sheet pp1–2 | Invitations, named stages, offers and returning-fan execution; complete authoritative asset/step map still absent |
| Cristina's four PPVs per level, no skipped levels/scripts | Cristina sheet pp1–2 | Multi-card sequences and resumed progression; no reliable per-card unlock links |
| Known no-push and product-preference exceptions | Amelia p4; Cristina p5 | Ordinary relationship threads, rejection and ownership complaints |
| Accurate buying information and unavailable-content answers | Authenticity Protocol p5, C1–C3 | Specific requests, disclosed alternatives, and contrary examples to reject |
| Routine content-complaint recovery within approved limits | Protocol p6, C4 | Resends, zero-price corrections, alternatives and unresolved obligations |
| Verify owned content and previous promises | Response Playbook p3, scenarios15/19 | Already-owned complaints, delayed extras and repeated cards |
| Ordinary chat when requested | Playbook p2, scenario13 | Long rapport threads and fans who explicitly do not want another sales pitch |
| No guilt, financial pressure or emotional leverage | GFE pp2,5–8 | Historical pressure and hardship cases to exclude; optional voluntary tips also present |
| Truthful identity handling, privacy and escalation | Protocol pp1–4,7–8 | Welcome disclosures, identity challenges and privacy risks |

The human-chatter Protocol's A2 example says the operator is not a bot. That statement is contextually different when the operator is AI. It cannot be copied as a false denial. Preserve the document's overarching truthfulness requirement and resolve the AI-specific response explicitly before launch. This is an applicability conflict, not permission to invent reassurance.

The creator sheets' content-reference pages are placeholders. Their references to existing vault scripts/sequences are real, but the five PDFs do not themselves enumerate every product, media ID, step price or current fan completion. Amelia's broad restrictions and VIP-only list must be applied to actual assets; an older caption cannot override a hard restriction.

## 5. What is still missing for dependable operation

The export is sufficient for this behavioural analysis and a substantial evaluation set. It is not sufficient, by itself, to operate exact purchases, ownership and fulfilment.

1. **Exact payment/unlock links.** All 208,334 message `purchased_at` fields are blank. Only two transaction rows have message IDs, both tips; there are zero explicit PPV-to-card joins. A same-fan/same-price/time candidate is not a verified purchase.
2. **Actual media and product mapping.** There are counts, but no attachment IDs, formats, durations, files, approved descriptions, sequence membership or live availability. Captions do not prove asset contents or ownership overlap.
3. **Current operational sequences and fan state.** The existing ordered scripts, prices, entry/completion rules, VIP status, pinned notes, no-push restrictions and unresolved orders must be linked to stable account/fan/product IDs. Labels embedded in old transaction descriptions are clues, not a current state table.
4. **Send ownership and campaign provenance.** Missing sender/campaign identifiers prevent clean human-versus-AI attribution and exact comparison of one-to-one selling with promotional distribution.
5. **Capture and ledger semantics.** The files are the database's saved rows, not demonstrated complete provider history. 96.22% of messages are from October/November. Only 1,265 of 9,437 account/fan pairs with transactions have any captured messages. Timezone, currency, stable provider transaction IDs, settlement meanings and reversal links remain missing.

Several captured threads contain replies to unseen messages; some fans explicitly mention disappearing or deleted messages. That limits any claim of abandonment, unanswered intent or no sale based solely on a gap. It does not erase the visible failures, but it prevents inventing an underlying scheduler cause.

The supplied renderer labels blank purchase timestamps as UNPAID and sums transaction statuses together. Those outputs must not be used as a verified ledger. Pending does not automatically mean unpaid, and cleared totals are not net collected revenue without currency, fees and reversal reconciliation.

**Correction to the initial report:** the existing payment-candidate CSV contains 376 single-candidate PPV rows, 10 multiple-candidate rows and 24,591 with no candidate under its 24-hour heuristic. The earlier prose said nine and 24,592. This correction changes no verified-purchase conclusion: there are still zero exact PPV-to-card joins. Possible duplicate tip events also remain unresolved.

Maile/Mally is absent from this Drive export. The review is complete for the supplied Amelia/Cristina files; it does not claim a fresh complete review of Maile's separate history or an entire one-to-two-year provider archive.

## 6. What this means for the new Moxy reproduction

The target remains the same: implement Moxy's existing authorised process, using its creator sheets and real executions. This review does not introduce a competing sales strategy or authorise changes to the original repair branch.

The evidence now supports a concrete mapping: welcome/outreach → relevant invitation or direct request → eligible product/sequence offer → actual purchase/delivery result → the appropriate continuation, service fulfilment or repair. Ordinary chat, no-push fans and human escalation remain valid outcomes under the documents. Promotional activity needs the same ownership and purchase protections as individual replies.

Next build preparation should connect the missing existing operational records, preserve source references for adopted behaviour, and turn the cases below into evaluation scenarios. Use historical failures as negative cases. Do not restart broad importing, call paid models, invent sequence prices, or deploy anything merely to fill these gaps.

## 7. Evidence and reproducibility

The accompanying complete package includes:

- This report, full review notes, progress and verified coverage totals.
- All 2,214 two-way coverage records and all 28,312 one-way member records.
- All 84 review batches, the compact reader, and the original-message index.
- A focused case register with source message IDs and PDF-page references.
- The five internal-caption mismatch records.
- The three unchanged compressed source datasets, their hashes, the original source scripts, earlier numerical analysis and timing observations.
- The five supplied PDFs and extracted text when available in the saved workspace; they remain source material, not newly approved policy.

Run `verify_coverage.py` from the review directory to reconcile the records. `prepare_review.py` is preserved only for reproducibility: running it regenerates the presentation and resets review-status fields, so it must not be run over the completed review directory. The earlier 18-case report is retained as superseded history.

Evidence references use account/fan IDs and captured sequence ordinals; `source_message_index.csv` resolves them to original message IDs and times. Price examples preserve exported integer cents and the transcript's dollar-style presentation; the actual settlement currency was not independently established.

Source PDFs: [Amelia](https://chatgpt.com/library?file_id=libfile_0ae53f4791548191bc7a5594a5a749e4&account_id=personal), [Cristina](https://chatgpt.com/library?file_id=libfile_ef12b6f920608191b3b29616d93940dc&account_id=personal), [GFE boundaries](https://chatgpt.com/library?file_id=libfile_d1d528580d50819184964bc3de592862&account_id=personal), [Authenticity Protocol](https://chatgpt.com/library?file_id=libfile_5f2182a39c3881918b252c8277e23125&account_id=personal), [Response Playbook](https://chatgpt.com/library?file_id=libfile_cb2155d7725481919dd01863e2e77b6a&account_id=personal).
