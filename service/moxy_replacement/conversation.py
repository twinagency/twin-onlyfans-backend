"""Evidence boundaries for incoming batches and the observed attribution failures.

These are bounded factual screens, not a general semantic entailment classifier.
The writer must still answer the whole input batch and use the actual speakers.
"""
import re

# Recognise an already-sent legacy fallback as a non-answer, never generate it.
LEGACY_TECHNICAL_FALLBACK="I'm having trouble replying right now. Could you give me a moment?"
_TECHNICAL=re.compile(r"\b(?:having trouble|unable to|can'?t|cannot) (?:reply|replying|respond|responding)\b|\btechnical (?:issue|problem|difficult)|\b(?:system|server) (?:glitch|error|problem)",re.I)

def is_technical_fallback(text):
    return bool(_TECHNICAL.search(text or ''))


def pending_messages(snapshot):
    if 'pending_fan_messages' in snapshot:return snapshot['pending_fan_messages']
    result=[]
    for m in reversed(snapshot.get('history',[])):
        if m['direction']=='out' and not is_technical_fallback(m.get('text')):break
        if m['direction']=='in':result.append(m)
    return list(reversed(result))


def _norm(text):
    text=text.lower().replace('’',"'")
    for before,after in [("i'm","i am"),("you're","you are"),("i'd","i would"),("you'd","you would"),("i've","i have"),("you've","you have"),("i'll","i will"),("you'll","you will"),("didn't","did not"),("don't","do not"),("wouldn't","would not"),("won't","will not"),("can't","cannot"),("shouldn't","should not")]:
        text=re.sub(r'\b'+re.escape(before)+r'\b',after,text)
    text=re.sub(r'\b(?:i|me)\b','you',text);text=re.sub(r'\bmy\b','your',text)
    text=re.sub(r'\byou am\b','you are',text);text=re.sub(r'\byou was\b','you were',text)
    return ' '.join(re.findall(r"[\w']+",text))


# A quote is not automatically a proposition asserted by its writer. In
# particular, denying or retracting quoted speech cannot establish that speech.
_QUOTES=re.compile(r'"([^"\n]+)"|(?<!\w)\'([^\n]+?)\'(?!\w)')
_ATTRIBUTION=re.compile(r"\byou\s+(?:(?:also|then|earlier|previously|once)\s+)?(?:said|told\s+me|mentioned|wrote|were\s+saying)(?:\s*,\s*|\s+)(?:that\s+)?",re.I)
_NEGATIVE_ATTRIBUTION=re.compile(r"\byou\s+(?:didn't|did\s+not|never)\s+(?:say|tell\s+me|mention|write)\s+(?:that\s+)?",re.I)
_NEGATED_SPEECH=re.compile(r"\b(?:i|you)\s+(?:(?:did\s+not|didn't|never|do\s+not|don't|wasn't|was\s+not)\s+(?:say|said|tell|told|mean|claim|write|mention)|(?:am\s+not|am\s+never|'m\s+not)\s+saying)\b",re.I)
_RETRACT=re.compile(r"\bi\s+(?:(?:was|am)\s+(?:wrong|mistaken)|(?:shouldn't|should\s+not)\s+have|(?:didn't|did\s+not)\s+mean\s+to)\b",re.I)
_OTHER_SPEECH=re.compile(r"\b(?:you|he|she|they)\s+(?:(?:also|then|earlier|previously|once)\s+)?(?:said|told|mentioned|wrote)|\b(?:your|his|her|their)\s+(?:words?|line|statement)\b",re.I)
_RELATIONSHIP=re.compile(
    r"\bwe\s+(?:only\s+)?(?:just\s+)?(?:first\s+)?(?:met|started\s+(?:talking|chatting)|began\s+(?:talking|chatting))\b"
    r"|\b(?:we(?:'ve| have)|we\s+have)\s+(?:only\s+)?been\s+(?:talking|chatting)\s+(?:for|since)\b"
    r"|\bwe\s+(?:haven't|have not)\s+(?:even\s+)?(?:had|made it through)\s+(?:one|a)\s+(?:full\s+)?conversation\b",re.I)
_UNCERTAIN=re.compile(r"\b(?:can'?t|cannot)(?:\s+(?:actually|even))?\s+(?:say|tell|see|pin(?:point)?(?:\s+down)?|know|remember|give)|\b(?:don'?t|do not)\s+know|\bnot sure",re.I)
_UNASSERTED_FAN=re.compile(r"\b(?:i|we)\s+(?:(?:didn't|did not|don't|do not|never|cannot|can't|am not|'m not)\b|(?:doubt|wonder|wish|imagine|asked)\b)|^\s*(?:if|unless|when|why|what|how|did|do|does|would|could|should|can)\b|\b(?:my|your|a|the|our)\s+\w+(?:\s+\w+){0,3}\s+(?:said|told|mentioned|wrote)\b",re.I)
_CLAUSE_BOUNDARY=re.compile(r"(?<!\d)\.|\.(?!\d)|[!?;\n]+|,\s*(?:(?:and|but|yet|also)\s+)?|\b(?:but|yet|and)\s+(?=(?:you\s+(?:said|told|mentioned|wrote)|we\s+(?:met|started|began)))",re.I)


def quotes(text):
    return [(m.start(),m.end(),m.group(1) or m.group(2)) for m in _QUOTES.finditer(text.replace('“','"').replace('”','"').replace('’',"'"))]


def clauses(text):
    """Split independent clauses outside quotations; keep quotation scope intact."""
    text=text.replace('“','"').replace('”','"').replace('’',"'")
    spans=quotes(text);start=0
    for match in _CLAUSE_BOUNDARY.finditer(text):
        if any(a<=match.start()<b for a,b,_ in spans):continue
        if match.group().startswith(',') and re.search(r'\b(?:said|wrote|told|meant)\s*$',text[start:match.start()],re.I) and text[match.end():].lstrip().startswith(('\'','"')):continue
        if text[start:match.start()].strip():yield text[start:match.start()].strip()
        start=match.end()
    if text[start:].strip():yield text[start:].strip()


def _affirmed_fan_spans(text):
    for clause in clauses(text):
        # Denial/retraction governs its embedded quote. A later independent
        # affirmative clause is considered separately, never as a substring.
        if _NEGATED_SPEECH.search(clause) or _RETRACT.search(clause) or _OTHER_SPEECH.search(clause) or _UNASSERTED_FAN.search(clause) or (text.rstrip().endswith('?') and clause==list(clauses(text))[-1]):
            yield _norm(clause),False
            continue
        found=quotes(clause)
        if found:
            if re.search(r"\bi\s+(?:said|wrote|told|meant)\b",clause,re.I):
                for _,_,quote in found:yield _norm(quote),True
            # Quoted third-party words or quoted hypotheticals are not an
            # affirmative fan statement. The remaining unquoted words can be.
            for a,b,_ in reversed(found):clause=clause[:a]+' '+clause[b:]
        if clause.strip():yield _norm(clause),True


def _fan_spans(snapshot):
    incoming={m['id']:m for m in snapshot.get('history',[])+pending_messages(snapshot) if m['direction']=='in'}
    return [span for m in incoming.values() for span in _affirmed_fan_spans(m.get('text',''))]


def _supported(claim,sources):
    claim=_norm(claim)
    return bool(claim and claim not in {'it','that','so'} and any(
        claim==source or (affirmed and (' '+claim+' ') in (' '+source+' ')) for source,affirmed in sources))


def _claimed_words(clause,match,end):
    tail=clause[match.end():end].strip()
    found=quotes(tail)
    if found and found[0][0]==0:
        return found[0][2],tail[found[0][1]:].strip()
    # Vocatives and emoji are not part of the attributed proposition.
    tail=re.split(r'\s+(?:babe|baby)\b',tail,maxsplit=1,flags=re.I)[0]
    return tail,''


def history_problem(snapshot,texts):
    fan_spans=_fan_spans(snapshot)
    for text in texts:
        if is_technical_fallback(text):return 'customer_facing_technical_fallback'
        for clause in clauses(text):
            quote_spans=quotes(clause)
            def known_creator_quote(position):
                for a,b,quote in quote_spans:
                    if a<=position<b and re.search(r'\b(?:i said|my (?:words|line|claim))\b',clause[:a],re.I):
                        return any(m.get('direction')=='out' and _norm(quote) in _norm(m.get('text','')) for m in snapshot.get('history',[]))
                return False
            for denial in _NEGATIVE_ATTRIBUTION.finditer(clause):
                if known_creator_quote(denial.start()):continue
                if _RETRACT.search(clause[:denial.start()]):continue
                claim=clause[denial.end():].strip()
                if _supported(claim,[(span,True) for span,affirmed in fan_spans if affirmed]):
                    return 'unsupported_fan_attribution'
            matches=list(_ATTRIBUTION.finditer(clause))
            quoted_ranges=[(a,b) for a,b,_ in quotes(clause)]
            for index,match in enumerate(matches):
                prefix=clause[:match.start()]
                # "I was wrong when I said you said ..." withdraws that
                # attribution. A subsequent comma/but clause is checked anew.
                if _RETRACT.search(prefix) or _NEGATED_SPEECH.search(prefix):continue
                if any(a<=match.start()<b for a,b in quoted_ranges):
                    # Quoting a mistaken earlier line in a correction does
                    # not assert that the fan actually said it.
                    if _RETRACT.search(prefix) or known_creator_quote(match.start()):continue
                claim,remainder=_claimed_words(clause,match,matches[index+1].start() if index+1<len(matches) else len(clause))
                if not _supported(claim,fan_spans):return 'unsupported_fan_attribution'
                # Never silently discard text after a closing quotation.
                if remainder and _RELATIONSHIP.search(remainder):return 'unsupported_relationship_history'
            for relation in _RELATIONSHIP.finditer(clause):
                prefix=clause[:relation.start()]
                if _RETRACT.search(prefix) or _UNCERTAIN.search(prefix):continue
                # An evidence-backed fan quotation reports what he said,
                # rather than establishing a relationship-start date.
                if any(a<=relation.start()<b for a,b in quoted_ranges) and matches:continue
                return 'unsupported_relationship_history'
    return None


def speaker_evidence(snapshot,texts):
    """Related creator utterances are context, never substitute fan evidence."""
    claims=[]
    for text in texts:
        for clause in clauses(text):
            for match in _ATTRIBUTION.finditer(clause):
                claims.append(set(_norm(clause[match.end():]).split())-{'you','your','a','the','to','me','on','i','said','told','would','before','after','babe'})
    rows=[]
    for m in snapshot.get('history',[]):
        if m['direction']!='out' or _ATTRIBUTION.search(m.get('text','')):continue
        tokens=set(_norm(m.get('text','')).split())
        if any(len(claim & tokens)>=2 for claim in claims):rows.append({'message_id':m['id'],'speaker':'creator','text':m['text']})
    return {'fan_statement_supported':False,'related_creator_statements':rows}


def unverified_history_claims(snapshot):
    result=[]
    for m in snapshot.get('history',[])[-20:]:
        if m['direction']!='out':continue
        problem=history_problem(snapshot,[m.get('text','')])
        if problem is None:continue
        record={'message_id':m['id'],'speaker':'creator','text':m['text'],'problem':problem}
        if problem=='unsupported_fan_attribution':record.update(speaker_evidence(snapshot,[m['text']]))
        result.append(record)
    return result
