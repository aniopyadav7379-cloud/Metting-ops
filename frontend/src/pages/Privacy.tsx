/**
 * Privacy Policy for Meeting-Ops, operated by Magic Unicorn
 * Unconventional Technology & Stuff Inc. (SC C-Corp).
 *
 * Covers GDPR (EU data subjects), CCPA (California residents),
 * and HIPAA scope (Enterprise + signed BAA only).
 *
 * Rendered at `/privacy`, publicly accessible without authentication.
 */
import React from 'react';
import LegalPage from '../components/LegalPage';

export const Privacy: React.FC = () => {
  return (
    <LegalPage
      title="Privacy Policy"
      effectiveDate="June 1, 2026"
      version="1.0"
    >
      <p>
        Magic Unicorn Unconventional Technology &amp; Stuff Inc. (&ldquo;
        <strong>Magic Unicorn</strong>,&rdquo; &ldquo;<strong>we</strong>
        ,&rdquo; &ldquo;<strong>us</strong>,&rdquo; &ldquo;
        <strong>our</strong>&rdquo;) operates Meeting-Ops (the &ldquo;
        <strong>Service</strong>&rdquo;). This Privacy Policy explains what
        information we collect, how we use it, who we share it with, and
        what choices you have.
      </p>

      <h2>1. Scope</h2>
      <p>
        This Policy applies to Meeting-Ops and to sibling Unicorn Commander
        Suite applications (for example, Project-Ops, Contact-Ops,
        Crisis-Ops, Brigade) to the extent they are integrated with the
        Service and share account or content data. Sibling applications
        may publish their own privacy policies that apply when you use them
        directly.
      </p>

      <h2>2. Information We Collect</h2>

      <h3>2.1 Account information</h3>
      <p>
        When you create an account we collect your email address, a
        username, and optionally your name. For organization accounts we
        also collect organization name and the role of each member.
      </p>

      <h3>2.2 Subscription &amp; billing</h3>
      <p>
        Payments are processed by Stripe, Inc. We do not receive or store
        your full payment-card number. We do receive subscription metadata
        (plan, billing period, status, last four digits of the card,
        country, postal code) to operate billing and to enforce tier
        access.
      </p>

      <h3>2.3 User Content (paid tiers only)</h3>
      <p>
        On paid tiers, you may send meeting audio, transcripts, summaries,
        action items, speaker labels, attachments, and related metadata to
        our servers for processing and storage. On the <strong>Free
        tier</strong>, audio, transcripts, and summaries never leave your
        device; we do not receive User Content from Free-tier sessions.
      </p>

      <h3>2.4 Usage &amp; diagnostic information</h3>
      <p>
        We collect server-side usage logs (URL path, method, status code,
        timing), application error logs, and session diagnostics needed to
        operate and secure the Service. We do not load third-party web
        analytics or advertising trackers on Service pages by default.
      </p>

      <h3>2.5 Device &amp; network information</h3>
      <p>
        We receive your IP address and a coarse approximation of your
        location through our network proxy (Cloudflare), as well as your
        browser type, operating system, and language preferences.
      </p>

      <h2>3. How We Use Information</h2>
      <ul>
        <li>To provide, operate, and maintain the Service;</li>
        <li>To process payments and manage subscriptions;</li>
        <li>To respond to your support requests;</li>
        <li>
          To detect, prevent, and address fraud, abuse, and security
          incidents;
        </li>
        <li>
          To improve the Service in aggregate (we do <strong>not</strong>{' '}
          use your User Content to train AI or ML models without your
          explicit opt-in);
        </li>
        <li>To comply with legal obligations.</li>
      </ul>

      <h2>4. Sharing</h2>
      <p>We share personal information only as follows:</p>
      <ul>
        <li>
          <strong>Stripe, Inc.</strong> &mdash; payment processing.
        </li>
        <li>
          <strong>Postmark (ActiveCampaign LLC).</strong> &mdash;
          transactional email only: receipts, verification, password
          resets, security notifications. Not marketing.
        </li>
        <li>
          <strong>Cloudflare, Inc.</strong> &mdash; CDN, DNS, and
          DDoS-mitigation proxy in front of the Service.
        </li>
        <li>
          <strong>PostHog, Inc.</strong> &mdash; product analytics
          (anonymized event data; no audio or transcripts). You can opt
          out in Settings &rarr; Privacy.
        </li>
        <li>
          <strong>Umami.</strong> &mdash; pageview analytics, self-hosted
          on Magic Unicorn infrastructure with no third-party trackers.
        </li>
        <li>
          <strong>Service providers</strong> bound by written data
          processing addenda or equivalent contractual safeguards
          (categories: hosting, observability, security). We will provide
          a current list on written request.
        </li>
        <li>
          <strong>Legal process.</strong> We may disclose information when
          required by valid legal process or to protect our rights, your
          safety, or the safety of others, applying scrutiny appropriate
          to the request.
        </li>
        <li>
          <strong>Business transfers.</strong> In connection with a
          merger, acquisition, financing, reorganization, or sale of
          assets, information may be transferred. We will give you notice
          of any material change to data handling.
        </li>
      </ul>
      <p>
        <strong>
          We do not sell your personal information, and we do not share
          your personal information for cross-context behavioral
          advertising.
        </strong>
      </p>

      <h2>5. Your Choices</h2>
      <ul>
        <li>
          <strong>Access &amp; export.</strong> You may export your User
          Content at any time through the Customer Portal or the
          documented export endpoints.
        </li>
        <li>
          <strong>Delete account.</strong> Account deletion is available
          in Settings, or by emailing{' '}
          <a href="mailto:support@unicorncommander.ai">
            support@unicorncommander.ai
          </a>
          .
        </li>
        <li>
          <strong>Marketing email.</strong> Where we send marketing email,
          you may opt out at any time through the unsubscribe link.
          Transactional email (receipts, security notices, verification)
          is required to operate the Service and cannot be opted out of
          while you maintain an account.
        </li>
        <li>
          <strong>Privacy Mode.</strong> Pro-tier users may enable Privacy
          Mode to skip the server completion pass for individual sessions;
          nothing from those sessions leaves your device.
        </li>
      </ul>

      <h2>6. Data Retention</h2>
      <p>Retention mirrors the Terms of Service Section 9:</p>
      <ul>
        <li>
          <strong>Free:</strong> on-device only; server-side metadata up
          to 7 days.
        </li>
        <li>
          <strong>Basic:</strong> text artifacts for the life of your
          account; audio not retained server-side.
        </li>
        <li>
          <strong>Pro:</strong> text artifacts for the life of your
          account; compressed audio retained 90 days unless you download
          or export.
        </li>
        <li>
          <strong>Enterprise:</strong> per contract.
        </li>
      </ul>
      <p>
        After cancellation, account data is retained 30 days, then
        permanently deleted from primary systems. Backups and audit logs
        may persist longer where required by law, security, or
        disaster-recovery practice.
      </p>

      <h2>7. International Transfers</h2>
      <p>
        We operate the Service from servers in the United States and the
        European Union. If you access the Service from outside these
        regions, your information may be transferred to and processed in
        the United States or the European Union. For transfers of personal
        data from the European Economic Area, the United Kingdom, or
        Switzerland to the United States, we rely on the European
        Commission&rsquo;s Standard Contractual Clauses or equivalent
        approved transfer mechanisms.
      </p>

      <h2>8. Your Rights under GDPR (EEA, UK, Switzerland)</h2>
      <p>
        If you are located in the EEA, the UK, or Switzerland, you have
        the right to: (a) access the personal data we hold about you;
        (b) request correction of inaccurate data; (c) request erasure;
        (d) request restriction of processing; (e) data portability;
        (f) object to processing on legitimate-interest grounds; and
        (g) withdraw consent where processing is based on consent. You
        also have the right to lodge a complaint with your local
        supervisory authority. Magic Unicorn acts as <strong>Data
        Controller</strong> for account and Service-operation data, and
        as Data Processor for User Content you submit.
      </p>

      <h2>9. Your Rights under CCPA / CPRA (California)</h2>
      <p>
        California residents have the right to: (a) know what personal
        information we collect and how it is used; (b) request access to,
        deletion of, or correction of personal information;
        (c) non-discrimination for exercising these rights; and (d) opt
        out of the sale or sharing of personal information. We do not
        sell personal information and we do not share personal
        information for cross-context behavioral advertising, so no
        opt-out is required to achieve that outcome &mdash; it is our
        default.
      </p>
      <p>
        To exercise any right, email{' '}
        <a href="mailto:privacy@unicorncommander.ai">
          privacy@unicorncommander.ai
        </a>
        . We may need to verify your identity before responding.
      </p>

      <h2>10. HIPAA Status</h2>
      <p>
        Meeting-Ops <strong>Free, Basic, and Pro</strong> tiers are{' '}
        <strong>not HIPAA-compliant</strong> and may not be used to
        process Protected Health Information (PHI). HIPAA-compliant
        processing is available only on the <strong>Enterprise</strong>{' '}
        tier <strong>with a signed Business Associate Agreement (BAA)
        </strong> in place prior to any PHI being submitted. If you
        process PHI on a non-Enterprise tier without a BAA, you are in
        violation of these Terms and the Acceptable Use Policy and you
        are solely responsible for the consequences.
      </p>

      <h2>11. Children</h2>
      <p>
        Meeting-Ops is not directed to children under 13, and we do not knowingly collect personal information from children under 13. In the European Economic Area, the United Kingdom, and Switzerland, we do not knowingly collect personal information from children below the applicable age of digital consent, which ranges from 13 to 16 depending on the country. Creating an account and purchasing a paid subscription require you to be at least 18. If you believe a child has provided us personal information, contact{' '}
        <a href="mailto:privacy@unicorncommander.ai">
          privacy@unicorncommander.ai
        </a>{' '}
        and we will delete it.
      </p>

      <h2>12. Security</h2>
      <p>
        We use industry-standard safeguards to protect personal
        information, including TLS in transit, encryption at rest for
        sensitive fields, the principle of least privilege for internal
        access, secret rotation, dependency scanning, and regular
        security reviews. <strong>No system is unhackable.</strong> We
        cannot guarantee absolute security, and we encourage you to use
        strong, unique passwords and to enable any available
        multi-factor authentication.
      </p>

      <h2>13. Cookies &amp; Local Storage</h2>
      <p>
        We use a small number of first-party cookies and browser storage
        items to operate the Service: an authentication session cookie,
        a CSRF token, language preference, and (for Free tier) cached
        local models and recordings in IndexedDB. You can clear these
        through your browser at any time, with the consequence that you
        will be signed out and any local Free-tier recordings will be
        removed.
      </p>

      <h2>14. Changes</h2>
      <p>
        We may update this Privacy Policy from time to time. For material
        changes we will provide at least <strong>thirty (30) days</strong>{' '}
        advance notice through the Service or by email. Continued use of
        the Service after the effective date constitutes acceptance.
      </p>

      <h2>15. Contact</h2>
      <p>
        Privacy questions, GDPR / CCPA requests, or other inquiries:{' '}
        <a href="mailto:privacy@unicorncommander.ai">
          privacy@unicorncommander.ai
        </a>
        . For general support, email{' '}
        <a href="mailto:support@unicorncommander.ai">
          support@unicorncommander.ai
        </a>
        .
      </p>

      <p className="text-xs text-zinc-500">
        Effective Date: June 1, 2026. Version 1.0.
      </p>
    </LegalPage>
  );
};

export default Privacy;
