/**
 * Terms of Service for Meeting-Ops, operated by
 * Magic Unicorn Unconventional Technology & Stuff Inc. (a South
 * Carolina C-Corporation).
 *
 * Drafted defensively for a SaaS launch with mandatory arbitration
 * under South Carolina law, AAA Commercial Rules, individual basis,
 * no class actions. Liability capped at the amount paid in the
 * prior 12 months ($100 floor for Free users).
 *
 * Refund Policy is embedded as Section 6 (not a separate page).
 *
 * This page is rendered at `/terms`, publicly accessible without
 * authentication.
 */
import React from 'react';
import LegalPage from '../components/LegalPage';

export const Terms: React.FC = () => {
  return (
    <LegalPage
      title="Terms of Service"
      effectiveDate="June 1, 2026"
      version="1.0"
    >
      <p>
        These Terms of Service (the &ldquo;<strong>Terms</strong>&rdquo;)
        govern your access to and use of Meeting-Ops (the &ldquo;
        <strong>Service</strong>&rdquo;), operated by{' '}
        <strong>
          Magic Unicorn Unconventional Technology &amp; Stuff Inc.
        </strong>
        , a South Carolina C-Corporation (&ldquo;<strong>Magic Unicorn</strong>
        ,&rdquo; &ldquo;<strong>we</strong>,&rdquo; &ldquo;<strong>us</strong>
        ,&rdquo; or &ldquo;<strong>our</strong>&rdquo;). By accessing or using
        the Service, you (&ldquo;<strong>you</strong>&rdquo; or &ldquo;
        <strong>User</strong>&rdquo;) agree to be bound by these Terms. If you
        do not agree, do not use the Service.
      </p>

      <h2>1. Acceptance of Terms</h2>
      <p>
        By creating an account, signing in, or otherwise using the Service,
        you accept these Terms and our <a href="#/privacy">Privacy Policy</a>{' '}
        and <a href="#/aup">Acceptable Use Policy</a>, each incorporated by
        reference. We may update these Terms from time to time. When we make
        material changes we will provide reasonable advance notice through the
        Service or by email to the address on file. Continued use of the
        Service after the effective date of an update constitutes acceptance.
      </p>

      <h2>2. Eligibility</h2>
      <p>
        You must be at least 18 years of age (or the age of majority in your
        jurisdiction, if higher), capable of forming a binding contract, and
        not barred from receiving services under the laws of the United
        States or other applicable jurisdiction. You represent that you are
        not on any U.S. or other government sanctions list, and that your use
        of the Service does not violate U.S. export controls or applicable
        sanctions regimes. If you are under 18, you may use the free on-device features of Meeting-Ops only with the consent and supervision of a parent or legal guardian who agrees to these Terms on your behalf. Meeting-Ops is not directed to children under 13, and we do not knowingly collect personal information from children under 13. In the European Economic Area, the United Kingdom, and Switzerland, you must be at least the age of digital consent in your country, which ranges from 13 to 16, and paid subscriptions still require you to be at least 18.
      </p>

      <h2>3. Account</h2>
      <p>
        To use most features of the Service you must create an account. You
        agree to provide accurate, current, and complete information and to
        keep that information current. You are responsible for all activity
        that occurs under your credentials, for maintaining the
        confidentiality of your password, and for notifying us promptly of
        any unauthorized access. One natural person or business entity per
        account. You may not transfer or sell your account.
      </p>

      <h2>4. The Service; Tiers</h2>
      <p>
        The Service is offered in tiers, including a Free tier, Basic, Pro,
        and Enterprise. Tier features are described on our pricing page and
        may change over time. Sibling applications in the Unicorn Commander
        Suite (for example, Project-Ops, Contact-Ops, Crisis-Ops, Brigade)
        are separate services with their own terms; your use of any such
        application is subject to its own terms in addition to these Terms
        when integrated with the Service.
      </p>

      <h2>5. Subscription &amp; Billing</h2>
      <p>
        Paid subscriptions are offered on a monthly or annual basis and{' '}
        <strong>auto-renew</strong> at the then-current rate at the end of
        each billing period unless cancelled before renewal. Prices and tier
        features may change; we will provide at least <strong>30 days</strong>{' '}
        advance notice of any price increase, and the new price applies to
        your next renewal. Payments are processed by Stripe, Inc. and are
        subject to Stripe&rsquo;s terms. You authorize us (through Stripe) to
        charge your designated payment method for all fees due. Taxes are
        your responsibility unless we are required to collect them, in which
        case applicable taxes will be added at checkout (handled via Stripe
        Tax where available).
      </p>

      <h2>6. Refund Policy</h2>
      <p>
        We offer the following limited refunds:
      </p>
      <ul>
        <li>
          <strong>Monthly subscriptions:</strong> A full refund is available
          within <strong>seven (7) days</strong> of your first monthly
          subscription, on request to{' '}
          <a href="mailto:support@unicorncommander.ai">
            support@unicorncommander.ai
          </a>
          . Subsequent monthly renewals are non-refundable.
        </li>
        <li>
          <strong>Annual subscriptions:</strong> A prorated refund is
          available within <strong>thirty (30) days</strong> of purchase or
          renewal, calculated based on the unused portion of the term, on
          request to{' '}
          <a href="mailto:support@unicorncommander.ai">
            support@unicorncommander.ai
          </a>
          .
        </li>
        <li>
          <strong>Enterprise contracts:</strong> Refunds are governed
          exclusively by the executed contract. No refund is available except
          as expressly provided therein.
        </li>
        <li>
          <strong>AUP violations:</strong> No refund is available where an
          account is suspended or terminated for violation of the{' '}
          <a href="#/aup">Acceptable Use Policy</a>, for fraud, chargebacks,
          or where required by law.
        </li>
      </ul>
      <p>
        All refund requests must be sent to{' '}
        <a href="mailto:support@unicorncommander.ai">
          support@unicorncommander.ai
        </a>
        . Refunds are processed back to the original payment method. We
        retain sole discretion over edge cases and exceptional circumstances.
      </p>

      <h2>7. Acceptable Use</h2>
      <p>
        Your use of the Service is governed by our{' '}
        <a href="#/aup">Acceptable Use Policy</a>, incorporated by reference.
        Violations may result in immediate suspension or termination without
        refund.
      </p>

      <h2>8. User Content</h2>
      <p>
        &ldquo;<strong>User Content</strong>&rdquo; means the meetings,
        audio recordings, transcripts, summaries, action items, speaker
        labels, attachments, and any other content you submit to or generate
        through the Service.
      </p>
      <p>
        <strong>You own your User Content.</strong> You grant Magic Unicorn a
        worldwide, non-exclusive, royalty-free license to host, store,
        process, transmit, transcribe, summarize, and display your User
        Content <strong>solely to provide the Service to you</strong>. We do
        not use your User Content for marketing, and we do not use it to
        train artificial intelligence or machine learning models without
        your explicit, separate opt-in. The license terminates when you
        delete the content or your account, subject only to the retention
        periods described in Section 9 and to retention required for legal,
        audit, security, or backup purposes.
      </p>

      <h2>9. Data Retention</h2>
      <ul>
        <li>
          <strong>Free tier:</strong> Audio, transcripts, and summaries
          remain on your device indefinitely (browser-only). Server-side
          metadata is retained no more than <strong>seven (7) days</strong>.
        </li>
        <li>
          <strong>Basic tier:</strong> Text artifacts (transcripts,
          summaries, action items) are retained for the life of your
          account. Audio is not retained server-side on Basic.
        </li>
        <li>
          <strong>Pro tier:</strong> Text artifacts are retained for the
          life of your account. Compressed audio is retained for{' '}
          <strong>ninety (90) days</strong> unless you download or export
          it, after which it may be removed from primary storage.
        </li>
        <li>
          <strong>Enterprise tier:</strong> Retention is governed by your
          contract.
        </li>
      </ul>
      <p>
        On cancellation, account data is retained for <strong>thirty (30)
        days</strong> to allow reactivation, then permanently deleted from
        primary systems. Backups and audit logs may persist longer where
        required for legal, security, or disaster-recovery purposes;
        retention in such systems does not constitute continued processing.
      </p>

      <h2>10. Free Tier Limitations</h2>
      <p>
        The Free tier runs entirely in your browser using locally-cached
        models. Transcription and summarization quality on Free is bounded
        by your device&rsquo;s compute. There is <strong>no SLA</strong> on
        the Free tier. We may rate-limit or restrict Free-tier traffic to
        protect Service availability. Free is genuinely free; we do not
        upsell aggressively or covertly degrade Free in order to push you to
        a paid tier.
      </p>

      <h2>11. Service Modifications</h2>
      <p>
        We may add, change, or remove features at any time. For
        <strong> material deprecations</strong> of paid-tier features, we
        will provide at least <strong>sixty (60) days</strong> advance
        notice. Where a deprecation materially reduces the value of a paid
        plan to which you are subscribed, you may cancel the plan with a
        prorated refund of unused fees.
      </p>

      <h2>12. Suspension &amp; Termination</h2>
      <p>
        We may suspend or terminate your access for any of the following:
        violation of these Terms or the AUP; non-payment; suspected fraud or
        abuse; court order or other legal process; or to protect the
        Service, our users, or third parties. You may cancel your
        subscription at any time through the Customer Portal or by emailing{' '}
        <a href="mailto:support@unicorncommander.ai">
          support@unicorncommander.ai
        </a>
        . Cancellation takes effect at the end of the then-current billing
        period; you retain access until then.
      </p>

      <h2>13. Disclaimers</h2>
      <p>
        THE SERVICE IS PROVIDED &ldquo;AS IS&rdquo; AND &ldquo;AS
        AVAILABLE,&rdquo; WITHOUT WARRANTIES OF ANY KIND, EXPRESS OR
        IMPLIED, INCLUDING WITHOUT LIMITATION WARRANTIES OF
        MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE,
        NON-INFRINGEMENT, OR COURSE OF PERFORMANCE. WE DO NOT WARRANT THAT
        THE SERVICE WILL BE UNINTERRUPTED, ERROR-FREE, OR SECURE.
      </p>
      <p>
        <strong>
          Transcription, summarization, and AI-generated output are
          best-effort and may contain errors.
        </strong>{' '}
        You should not rely on the Service for legal, medical, financial,
        safety-critical, or other consequential decisions without
        independent human verification.
      </p>

      <h2>14. Limitation of Liability</h2>
      <p>
        TO THE FULLEST EXTENT PERMITTED BY APPLICABLE LAW, IN NO EVENT WILL
        MAGIC UNICORN, ITS OFFICERS, DIRECTORS, EMPLOYEES, OR AGENTS BE
        LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL,
        EXEMPLARY, OR PUNITIVE DAMAGES, OR FOR LOST PROFITS, REVENUE, DATA,
        GOODWILL, OR BUSINESS OPPORTUNITY, ARISING OUT OF OR IN CONNECTION
        WITH THESE TERMS OR THE SERVICE, EVEN IF ADVISED OF THE POSSIBILITY
        OF SUCH DAMAGES.
      </p>
      <p>
        OUR AGGREGATE LIABILITY ARISING OUT OF OR RELATED TO THESE TERMS OR
        THE SERVICE WILL NOT EXCEED THE GREATER OF (A) THE AMOUNT YOU PAID
        US FOR THE SERVICE IN THE TWELVE (12) MONTHS PRECEDING THE EVENT
        GIVING RISE TO THE CLAIM, OR (B) ONE HUNDRED U.S. DOLLARS ($100).
      </p>

      <h2>15. Indemnification</h2>
      <p>
        You agree to indemnify, defend, and hold harmless Magic Unicorn and
        its officers, directors, employees, and agents from any claims,
        liabilities, damages, losses, and expenses (including reasonable
        attorneys&rsquo; fees) arising out of or related to: (a) your use
        of the Service in violation of these Terms or applicable law;
        (b) your User Content, including any claim that your User Content
        infringes a third party&rsquo;s rights; (c) recording any
        individual in violation of applicable wiretap, eavesdropping, or
        consent laws; and (d) your violation of any rights of a third
        party.
      </p>

      <h2>16. Recording Consent</h2>
      <p>
        You are <strong>solely responsible</strong> for obtaining all
        consents required by applicable law from participants in any
        meeting you record or process using the Service. Many jurisdictions
        (including, but not limited to, California, Florida, Illinois,
        Maryland, Massachusetts, Montana, New Hampshire, Pennsylvania, and
        Washington, as well as most European Union member states) require
        the consent of <strong>all parties</strong> to a recording. Magic
        Unicorn provides a tool; we do not verify, and are not a party to,
        any consent you obtain or fail to obtain. See the{' '}
        <a href="#/aup">Acceptable Use Policy</a> for more.
      </p>

      <h2>17. Governing Law; Mandatory Binding Arbitration</h2>
      <p>
        These Terms are governed by the laws of the{' '}
        <strong>State of South Carolina</strong>, United States, without
        regard to conflict-of-laws principles.
      </p>
      <p>
        <strong>MANDATORY BINDING ARBITRATION.</strong> Except for claims
        for injunctive relief and small claims, you and Magic Unicorn agree
        that any dispute, claim, or controversy arising out of or relating
        to these Terms or the Service will be resolved by{' '}
        <strong>final, binding, individual arbitration</strong>{' '}
        administered by the American Arbitration Association
        (&ldquo;<strong>AAA</strong>&rdquo;) under its Commercial
        Arbitration Rules. The seat of arbitration is{' '}
        <strong>Charleston County, South Carolina</strong>. Judgment on
        the award may be entered in any court of competent jurisdiction.
      </p>
      <p>
        <strong>CLASS ACTION WAIVER.</strong> You and Magic Unicorn agree
        to bring any claim only in our individual capacities and not as a
        plaintiff or class member in any purported class, collective, or
        representative proceeding. The arbitrator may not consolidate
        claims or preside over any form of representative or class
        proceeding.
      </p>
      <p>
        Where injunctive or equitable relief is sought, or where claims are
        not subject to arbitration, the parties consent to the exclusive
        jurisdiction of the state and federal courts located in{' '}
        <strong>Charleston County, South Carolina</strong>.
      </p>

      <h2>18. Force Majeure</h2>
      <p>
        Neither party will be liable for any failure or delay in
        performance due to causes beyond its reasonable control, including
        acts of God, natural disasters, pandemics, war, terrorism, civil
        unrest, government action, labor disputes, internet or
        telecommunications failures, or third-party service-provider
        outages.
      </p>

      <h2>19. Assignment</h2>
      <p>
        We may assign these Terms or any rights hereunder, in whole or in
        part, without your consent, including in connection with a merger,
        acquisition, reorganization, or sale of assets. You may not assign
        these Terms without our prior written consent. Any prohibited
        assignment is void.
      </p>

      <h2>20. Entire Agreement; Severability; Headings</h2>
      <p>
        These Terms, together with the Privacy Policy and the Acceptable
        Use Policy, constitute the entire agreement between you and Magic
        Unicorn regarding the Service and supersede all prior
        understandings. If any provision is held unenforceable, the
        remaining provisions will continue in full force and effect.
        Headings are for convenience only and do not affect interpretation.
        Our failure to enforce any provision is not a waiver of our right
        to do so later.
      </p>

      <h2>21. Contact</h2>
      <p>
        Questions about these Terms? Contact us at{' '}
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

export default Terms;
