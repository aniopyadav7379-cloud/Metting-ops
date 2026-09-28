/**
 * Acceptable Use Policy for Meeting-Ops.
 *
 * The Recording Consent disclosure in Section 2 is the load-bearing
 * clause: it puts the burden of obtaining participant consent under
 * applicable law squarely on the user. The in-app participant
 * consent prompt UI is a separate feature (another sprint).
 *
 * Rendered at `/aup`, publicly accessible without authentication.
 */
import React from 'react';
import LegalPage from '../components/LegalPage';

export const AUP: React.FC = () => {
  return (
    <LegalPage
      title="Acceptable Use Policy"
      effectiveDate="June 1, 2026"
      version="1.0"
    >
      <p>
        This Acceptable Use Policy (&ldquo;<strong>AUP</strong>&rdquo;)
        governs your use of Meeting-Ops (the &ldquo;<strong>Service</strong>
        &rdquo;), operated by Magic Unicorn Unconventional Technology &amp;
        Stuff Inc. (&ldquo;<strong>Magic Unicorn</strong>&rdquo;). By
        accessing or using the Service you agree to this AUP, which is
        incorporated by reference into the{' '}
        <a href="#/terms">Terms of Service</a>.
      </p>

      <h2>1. No Illegal Use</h2>
      <p>
        You will not use the Service to violate any applicable law,
        regulation, or court order &mdash; including, without limitation,
        laws relating to recording and surveillance, fraud, intellectual
        property, export controls, sanctions, defamation, harassment, or
        unauthorized access to computer systems.
      </p>

      <h2>2. Recording Consent &mdash; Critical</h2>
      <p>
        <strong>
          You are solely responsible for obtaining all legally required
          consent from every participant in any meeting you record,
          transcribe, or otherwise process using the Service, in
          compliance with the laws of all jurisdictions of all
          participants.
        </strong>
      </p>

      <h3>2.1 Two-party-consent (all-party-consent) jurisdictions</h3>
      <p>
        In jurisdictions that require the consent of all parties to a
        recording &mdash; including, without limitation,{' '}
        <strong>
          California, Florida, Illinois, Maryland, Massachusetts, Montana,
          New Hampshire, Pennsylvania, and Washington
        </strong>
        , as well as most member states of the European Union and many
        other countries &mdash; you must obtain consent from{' '}
        <strong>every participant</strong> before recording begins.
      </p>

      <h3>2.2 One-party-consent jurisdictions</h3>
      <p>
        In jurisdictions that permit one-party consent, you may record
        with your own consent. If any participant is in a two-party
        jurisdiction, however, that jurisdiction&rsquo;s law may apply
        to you and require all-party consent.
      </p>

      <h3>2.3 Disclosure</h3>
      <p>
        You agree to display recording disclosure to participants when
        required by applicable law, including (where available) using the
        in-app consent prompts provided by the Service. You will not
        record in violation of any law against eavesdropping,
        wiretapping, interception, or similar.
      </p>

      <h3>2.4 No verification by Magic Unicorn</h3>
      <p>
        <strong>
          Magic Unicorn is not a party to your meetings, does not verify
          consent compliance on your behalf, and is not responsible for
          any failure on your part to obtain required consent.
        </strong>{' '}
        The Service provides recording and transcription tools; your use
        of those tools is your responsibility.
      </p>

      <h2>3. Prohibited Uses</h2>
      <p>You will not use the Service to:</p>
      <ul>
        <li>
          Harass, threaten, intimidate, defame, or abuse any person or
          group;
        </li>
        <li>
          Record in any place where participants have a reasonable
          expectation of privacy (including, without limitation,
          bathrooms, locker rooms, dressing rooms, hospital rooms,
          residences without consent);
        </li>
        <li>
          Record proceedings that are legally privileged or confidential
          without authorization (for example, attorney-client privileged
          communications, sealed court proceedings, executive session,
          grand-jury proceedings);
        </li>
        <li>
          Process Protected Health Information (PHI) on any tier other
          than Enterprise with a fully executed Business Associate
          Agreement (BAA);
        </li>
        <li>
          Process personal data of children under <strong>13</strong>{' '}
          (COPPA) or under <strong>16</strong> in the EEA without
          verifiable parental or guardian consent;
        </li>
        <li>
          Reverse engineer, decompile, disassemble, scrape, crawl, or
          otherwise attempt to extract source code or underlying ideas
          from the Service, except to the limited extent applicable law
          expressly permits and may not be contractually waived;
        </li>
        <li>
          Send, store, or distribute spam, malware, viruses, ransomware,
          phishing content, or other malicious code through the Service;
        </li>
        <li>
          Use the Service for cryptocurrency mining or any other
          unauthorized use of our compute resources;
        </li>
        <li>
          Circumvent or attempt to circumvent usage limits, rate limits,
          paywalls, tier gating, or authentication controls;
        </li>
        <li>
          Resell, white-label, or otherwise offer the Service to third
          parties as your own without a written reseller or partner
          agreement with Magic Unicorn;
        </li>
        <li>
          Impersonate any person or entity, or misrepresent your
          affiliation with any person or entity.
        </li>
      </ul>

      <h2>4. Enforcement</h2>
      <p>
        We may, in our sole discretion and without prior notice where
        appropriate, suspend or terminate accounts that violate this
        AUP. Termination for AUP violation does not entitle you to a
        refund of any fees paid. We may also be required to report
        certain conduct to law enforcement.
      </p>

      <h2>5. Reporting Abuse</h2>
      <p>
        To report a suspected AUP violation, security issue, or abuse,
        email{' '}
        <a href="mailto:abuse@unicorncommander.ai">
          abuse@unicorncommander.ai
        </a>
        . Include enough detail for us to investigate (URL, account,
        timestamp, description). We review reports promptly.
      </p>

      <h2>6. Changes</h2>
      <p>
        We may update this AUP from time to time. Material changes will
        be announced through the Service or by email and take effect on
        the date stated.
      </p>

      <p className="text-xs text-zinc-500">
        Effective Date: June 1, 2026. Version 1.0.
      </p>
    </LegalPage>
  );
};

export default AUP;
