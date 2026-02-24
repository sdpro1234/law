import json
import logging
import random
from flask import current_app

try:
    import google.generativeai as genai
except Exception:
    genai = None

from models import User
from sqlalchemy import or_


def configure_gemini():
    """Try to prepare and return either a client-style object or the genai module.

    Returns a tuple: (client_or_genai, is_client_bool). If nothing can be
    prepared, returns (None, None).
    """
    api_key = current_app.config.get('GEMINI_API_KEY') or current_app.config.get('AI_API_KEY')

    # Try to import the newer client-style module if available
    google_genai = None
    try:
        from google import genai as google_genai  # type: ignore
    except Exception:
        google_genai = None

    # 'genai' variable (imported at module top) may be the older style
    old_genai = genai

    if not api_key:
        # SDK present but no key configured is a normal dev situation; caller should fallback
        if google_genai or old_genai:
            logging.warning('Gemini SDK available but no API key configured; using fallback')
        return None, None

    # Prefer the client-style API when available
    if google_genai and hasattr(google_genai, 'Client'):
        try:
            client = google_genai.Client(api_key=api_key)
            logging.info('Initialized genai.Client (google.genai)')
            return client, True
        except Exception as e:
            logging.debug('Failed to init google.genai.Client: %s', e, exc_info=True)

    # Try the older style module if present
    if old_genai:
        # If old_genai exposes Client, try that too
        if hasattr(old_genai, 'Client'):
            try:
                client = old_genai.Client(api_key=api_key)
                logging.info('Initialized genai.Client (google.generativeai)')
                return client, True
            except Exception as e:
                logging.debug('Failed to init old genai.Client: %s', e, exc_info=True)

        # Otherwise try configure-style usage where we return the module
        if hasattr(old_genai, 'configure'):
            try:
                old_genai.configure(api_key=api_key)
                logging.info('Configured old google.generativeai module')
                return old_genai, False
            except Exception as e:
                logging.debug('Failed to configure old genai module: %s', e, exc_info=True)

    return None, None


def _keyword_fallback_struct(case_description):
    """Return a list of recommendation dicts matching the client expectation.
    Each recommendation is { 'specialization': str, 'reason': str }.
    """
    desc = (case_description or '').lower()
    suggestions = []

    def add_once(spec, reason, details=None):
        for s in suggestions:
            if s['specialization'] == spec:
                return
        suggestions.append({'specialization': spec, 'reason': reason, 'details': details or ''})

    if any(k in desc for k in ['property', 'real estate', 'lease', 'title', 'mortgage']):
        add_once('Real Estate Law', 'Matter involves property or real-estate disputes.', 'Look for title documents, lease agreements, property surveys, and any correspondence related to ownership or transactions.')
    if any(k in desc for k in ['contract', 'agreement', 'breach', 'terms']):
        add_once('Contract Law', 'Issue likely centers on contractual obligations.', 'Gather the contract, related amendments, emails about performance, payment records, and timelines of breaches or notices.')
    if any(k in desc for k in ['divorce', 'custody', 'family']):
        add_once('Family Law', 'Family law matters such as divorce or custody.', 'Collect marriage certificates, custody agreements, financial records, and any communications or evidence relevant to custody or support.')
    if any(k in desc for k in ['fraud', 'theft', 'assault', 'crime', 'criminal']):
        add_once('Criminal Law', 'Allegations of criminal conduct may require defense.', 'Preserve any evidence, list witnesses, note exact dates/times, and avoid discussing details publicly until counsel is engaged.')
    if any(k in desc for k in ['employment', 'wage', 'dismiss', 'harass']):
        add_once('Employment Law', 'Employment dispute or workplace-related claim.', 'Compile employment contracts, performance reviews, disciplinary records, pay slips, and any HR correspondence.')
    if any(k in desc for k in ['tax', 'irs', 'taxes']):
        add_once('Tax Law', 'Tax-related issues and filings.', 'Collect tax returns, notices from tax authorities, supporting documents for deductions, and payment records.')

    if len(suggestions) < 3:
        add_once('Civil Litigation', 'General civil litigation expertise for disputes.', 'Preserve evidence, identify potential defendants, document timelines, and consider statutory limitation periods.')

    return suggestions[:3]


def _fir_fallback_text(case_description, date_time=None, location=None):
    """Generate a structured FIR-style text from the provided description when AI is unavailable.
    This is deterministic and uses simple heuristics to fill common FIR fields.
    """
    import re
    desc = (case_description or '').strip()
    low = desc.lower()

    # Title heuristics + object extraction for specificity
    obj = None
    if re.search(r"\b(phone|mobile|cellphone|mobile phone|smartphone)\b", low):
        obj = 'Mobile Phone'
    elif re.search(r"\b(wallet|cash|money)\b", low):
        obj = 'Wallet/Cash'
    elif re.search(r"\b(laptop|notebook|computer)\b", low):
        obj = 'Laptop'
    elif re.search(r"\b(bike|motorbike|motorcycle|car|vehicle)\b", low):
        obj = 'Vehicle'

    if any(k in low for k in ['theft', 'stolen', 'robbery', 'lost']):
        title = 'Theft'
    elif any(k in low for k in ['assault', 'attack', 'battery']):
        title = 'Assault/Physical Injury'
    elif any(k in low for k in ['fraud', 'scam']):
        title = 'Fraud/Scam'
    elif any(k in low for k in ['domestic', 'divorce', 'custody']):
        title = 'Family Matter'
    else:
        title = 'Incident Report'

    # If we detected a specific object, include it in the title
    if obj:
        title_full = f"{title} - {obj}"
    else:
        title_full = title

    # Try to extract a location phrase like 'at X' or 'in X' or 'near X' unless caller provided it
    if not location:
        location = 'Unknown location'
        m = re.search(r"(?:at|in|near|on) ([A-Z][A-Za-z0-9_\-\. ]{2,60})", case_description)
        if m:
            location = m.group(1).strip()

    # Date/time fallback: allow caller to provide
    date_time = date_time or 'Unknown date/time (approximate when provided by client)'

    # Complainant details placeholder
    complainant = 'Complainant: Client (details provided at submission).'

    # Extract facts into bullet points
    facts = [s.strip() for s in re.split(r'[\.\n]+', desc) if s.strip()]
    if not facts:
        facts = ['Client provided limited details about the incident.']
    facts_text = '\n'.join(f'- {f}' for f in facts[:8])

    # Suspected persons / witnesses heuristics
    suspected = 'None identified' if not any(k in low for k in ['suspect', 'accused', 'known person', 'identified']) else 'See facts above'
    witnesses = 'None identified' if not any(k in low for k in ['witness', 'saw', 'witnesses']) else 'See facts above'

    # Losses / harms heuristics
    if any(k in low for k in ['phone', 'mobile', 'wallet', 'cash', 'money', 'laptop']):
        losses = 'Property loss reported (e.g., mobile phone, wallet or cash).'
    elif any(k in low for k in ['injury', 'bleeding', 'hurt', 'wounded']):
        losses = 'Physical injury reported.'
    else:
        losses = 'Losses or harms not clearly specified.'

    # Short title-based description derived from facts (1 sentence)
    title_description = facts[0] if facts else 'Brief incident summary unavailable.'
    # Trim to one concise sentence
    title_description = (title_description.split('.') or [title_description])[0].strip()

    report = (
        f"Incident Title: {title_full}\n"
        f"Title Description: {title_description}\n"
        f"Date & Time: {date_time}\n"
        f"Location: {location}\n"
        f"{complainant}\n\n"
        "Detailed Facts:\n"
        f"{facts_text}\n\n"
        f"Suspected Persons: {suspected}\n"
        f"Witnesses: {witnesses}\n"
        f"Immediate Losses/Harms: {losses}\n\n"
        "Recommended Next Steps:\n"
        "1. File this FIR at the nearest police station providing any evidence available (photos, receipts).\n"
        "2. Preserve and collect evidence: photos, serial numbers, receipts, CCTV if available.\n"
        "3. Obtain contact details of any witnesses and note exact times/locations.\n"
        "4. Contact a lawyer for guidance on criminal and civil remedies as appropriate.\n"
    )

    return report


def _parse_gemini_json_like(resp_json):
    """Try to extract a text payload from Gemini-like response and parse JSON out of it."""
    try:
        if isinstance(resp_json, str):
            # might already be a JSON string
            return json.loads(resp_json)
        candidates = resp_json.get('candidates')
        if candidates:
            text = candidates[0]['content']['parts'][0]['text']
            return json.loads(text)
    except Exception:
        return None

    return None


def get_ai_recommendations(case_description):
    """
    Returns a JSON string: {"recommendations": [ {specialization, reason}, ... ] }
    The function tries, in order:
      1. Use `current_app.generate_ai_response` if present.
      2. Use google.generativeai library (if installed and key configured).
      3. Deterministic keyword fallback.
    """
    # Build a concise prompt asking only for recommendations.
    prompt = (
        "You are an expert legal assistant. Based on the following case description, "
        "return a JSON object with 3 recommended lawyer specializations, a short reason for each, and an actionable `details` field explaining what documents or steps to prepare. "
        "Respond ONLY with JSON in the form: {\n  \"recommendations\": [ { \"specialization\": \"...\", \"reason\": \"...\", \"details\": \"...\" }, ... ]\n}\n\n"
        f"Case description: {case_description or ''}"
    )

    # 1) Try centralized helper if available
    try:
        helper = getattr(current_app, 'generate_ai_response', None)
        if helper:
            resp = helper(prompt)
            parsed = _parse_gemini_json_like(resp)
            if parsed and parsed.get('recommendations'):
                return json.dumps({"recommendations": parsed.get('recommendations')})
    except Exception:
        logging.debug('Central helper for recommendations failed', exc_info=True)

    # 2) Try direct genai library
    client_or_genai, is_client = configure_gemini()
    if client_or_genai:
        try:
            candidates = []
            pref = current_app.config.get('GEMINI_MODEL')
            if pref:
                candidates.append(pref)
            candidates.extend(['models/gemini-1.5-flash-latest', 'models/gemini-1.5-flash', 'gemini-1.5'])

            if is_client:
                client = client_or_genai
                for m in candidates:
                    try:
                        try:
                            out = client.models.generate_content(model=m, contents=prompt)
                        except TypeError:
                            out = client.models.generate_content(model=m, content=prompt)
                        parsed = _parse_gemini_json_like(out)
                        if parsed and parsed.get('recommendations'):
                            return json.dumps({"recommendations": parsed.get('recommendations')})
                    except Exception:
                        logging.debug('client generate attempt failed for model %s', m, exc_info=True)
            else:
                gen = client_or_genai
                for m in candidates:
                    try:
                        mdl = gen.GenerativeModel(m)
                        out = mdl.generate_content(prompt)
                        parsed = _parse_gemini_json_like(out)
                        if parsed and parsed.get('recommendations'):
                            return json.dumps({"recommendations": parsed.get('recommendations')})
                    except Exception:
                        logging.debug('configure-style generate failed for model %s', m, exc_info=True)
        except Exception:
            logging.warning('GenAI recommendations call failed', exc_info=True)

    # 3) Deterministic keyword fallback
    recommendations = _keyword_fallback_struct(case_description)
    return json.dumps({"recommendations": recommendations})


def get_ai_case_report_and_recommendations(case_description, mode=None, date_time=None, location=None):
    """
    Returns a JSON string with:
    {
        "case_report": "<generated report text>",
        "recommendations": [ {specialization, reason}, ... ]
    }

    Flow:
      1. Try centralized helper if present.
      2. Try Gemini API (client or configure) if available.
      3. Fallback to keyword-based recommendations + simple case summary.
    """
    if not case_description:
        case_description = ''

    # Support an FIR-style report mode when requested by the caller.
    if mode and str(mode).lower() == 'fir':
        # Ask the model to produce a police FIR-style report text.
        prompt = (
            "You are an expert legal assistant. Based on the following case description, "
            "generate a formal FIR-style report suitable for filing with local police. "
            "Include clear sections: Incident Title, Date & Time (approx), Location, Complainant details, "
            "Detailed facts (chronological), Suspected persons (if any), Witnesses (if any), "
            "Immediate harms/losses, and Recommended next steps for the complainant. "
            "Respond ONLY with plain text in a professional FIR/report tone (no JSON).\n\n"
            f"Case description: {case_description}"
        )
        fir_mode = True
    else:
        prompt = (
            "You are an expert legal assistant. Based on the following case description, "
            "1) Generate a concise case report (summary of facts, key issues, possible legal angles). "
            "2) Provide a JSON array of 3 recommended lawyer specializations with a short reason and an actionable `details` field for each recommendation. "
            "Respond ONLY with JSON in the following format:\n\n"
            "{\n"
            '  "case_report": "<text>",\n'
            '  "recommendations": [ { "specialization": "<spec>", "reason": "<reason>", "details": "<actionable details>" }, ... ]\n'
            "}\n\n"
            f"Case description: {case_description}"
        )
        fir_mode = False

    # 1) Try centralized helper if available
    try:
        helper = getattr(current_app, 'generate_ai_response', None)
        if helper:
            resp = helper(prompt)
            # If FIR mode, helper should return text; otherwise expect JSON-like
            if fir_mode:
                if resp and not (isinstance(resp, dict) and resp.get('error')):
                    text = resp if isinstance(resp, str) else str(resp)
                    # Return minimal JSON wrapper so callers can use case_report
                    return json.dumps({"case_report": text, "recommendations": _keyword_fallback_struct(case_description)})
            else:
                # helper may return dict with 'error'
                if isinstance(resp, dict) and resp.get('error'):
                    logging.debug('generate_ai_response returned error: %s', resp.get('error'))
                else:
                    parsed = _parse_gemini_json_like(resp)
                    if parsed and parsed.get('recommendations') and parsed.get('case_report'):
                        return json.dumps(parsed)
    except Exception as e:
        logging.debug('Central helper failed: %s', e, exc_info=True)

    # 2) Try direct genai library
    client_or_genai, is_client = configure_gemini()
    if client_or_genai:
        try:
            # prepare candidate model names
            candidates = []
            pref = current_app.config.get('GEMINI_MODEL')
            if pref:
                candidates.append(pref)
            candidates.extend(['models/gemini-1.5-flash-latest', 'models/gemini-1.5-flash', 'models/gemini-1.5'])

            if is_client:
                client = client_or_genai
                for m in candidates:
                    try:
                        try:
                            out = client.models.generate_content(model=m, contents=prompt)
                        except TypeError:
                            out = client.models.generate_content(model=m, content=prompt)

                        # If FIR mode, we expect plain text back
                        if fir_mode:
                            text = None
                            try:
                                if isinstance(out, str):
                                    text = out
                                else:
                                    parsed_text = None
                                    try:
                                        parsed_text = out.get('candidates')[0]['content']['parts'][0]['text']
                                    except Exception:
                                        pass
                                    if not parsed_text:
                                        parsed_text = getattr(out, 'text', None) or getattr(out, 'output', None)
                                    text = parsed_text
                            except Exception:
                                text = None
                            if text:
                                return json.dumps({"case_report": text, "recommendations": _keyword_fallback_struct(case_description)})
                        else:
                            parsed = _parse_gemini_json_like(out)
                            if parsed and parsed.get('recommendations') and parsed.get('case_report'):
                                return json.dumps(parsed)
                    except Exception:
                        logging.debug('client generate attempt failed for model %s', m, exc_info=True)

            else:
                gen = client_or_genai
                candidates = [pref] if pref else []
                candidates.extend(['models/gemini-1.5-flash-latest', 'models/gemini-1.5-flash', 'gemini-1.5-flash'])
                for m in candidates:
                    try:
                        mdl = gen.GenerativeModel(m)
                        out = mdl.generate_content(prompt)
                        if fir_mode:
                            text = None
                            try:
                                if isinstance(out, str):
                                    text = out
                                else:
                                    candidates_out = out.get('candidates') if isinstance(out, dict) else None
                                    if candidates_out:
                                        text = candidates_out[0]['content']['parts'][0]['text']
                                    else:
                                        text = getattr(out, 'text', None)
                            except Exception:
                                text = None
                            if text:
                                return json.dumps({"case_report": text, "recommendations": _keyword_fallback_struct(case_description)})
                        else:
                            parsed = _parse_gemini_json_like(out)
                            if parsed and parsed.get('recommendations') and parsed.get('case_report'):
                                return json.dumps(parsed)
                    except Exception:
                        logging.debug('configure-style generate failed for model %s', m, exc_info=True)
        except Exception as e:
            logging.warning('GenAI call failed: %s', e, exc_info=True)

    # 3) Fallback deterministic
    recommendations = _keyword_fallback_struct(case_description)
    # If FIR mode requested, produce a structured FIR-style fallback report
    if fir_mode:
        fir_text = _fir_fallback_text(case_description, date_time=date_time, location=location)
        return json.dumps({
            "case_report": fir_text,
            "recommendations": recommendations
        })

    # Default fallback: simple summary
    first_line = case_description.strip().split('\n')[0] if case_description.strip() else 'Client provided limited details.'
    case_report = f"Case Summary: {first_line}. Recommended to consult relevant legal experts based on the nature of the case."
    return json.dumps({
        "case_report": case_report,
        "recommendations": recommendations
    })
