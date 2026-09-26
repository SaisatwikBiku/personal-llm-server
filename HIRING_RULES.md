# Hiring rules

Employers reject or hold applications that break their own rules: the same posting twice, too many applications at one company, reapplying too soon after a rejection, applying outside a graduation window, ignoring an AI-use policy. The agent checks every application against the rules below so it never sends one that would count against me.

The rules are plain code in `jobs.py` (`RULES` and `check_rules()`), run over my job history, my profile, each posting's text and each form's questions. The model doesn't decide them, so the same application always gets the same result.

## When they run

1. When the Review list is built. Blocked jobs are left out, and the nightly run doesn't spend time preparing them.
2. When I tap Approve.
3. Right before the autofill submits an approved application, since another application may have gone out after I approved this one. A job blocked at this point stays approved but is held back from Apply to approved.

## Actions

| Action | What happens |
|---|---|
| Block | The job stays out of Review and Apply, and Approve refuses it. It still shows under New with a "Blocked: reason" tag. |
| Ask | Approve becomes Approve anyway. I confirm the reasons, and the job remembers which rules I overrode. |
| Warn | A note on the job. Nothing is stopped. |
| Off | The rule isn't checked. |

Each rule's action can be changed under You, Rules in the panel. The settings are stored in `jobs.json` (`meta["rules"]`).

## The rules

### Duplicates and volume

| Rule | Default | Breaks when |
|---|---|---|
| Same posting twice (`already_applied`) | Block | The job is already applied, interviewing, rejected or withdrawn. |
| Reposted opening (`repost`) | Ask | Another job at the company has the same title and location, and I approved, applied, interviewed or was rejected for it in the last 60 days. |
| Company application cap (`company_cap`) | Block | I already have the cap's number of approved, applied or interviewing jobs at the company within its window. A limit the company states itself wins: Ashby postings carry one (OpenAI: "Candidates may not apply more than 5 times in any 180 day span"), and the agent also reads phrases like "no more than three roles within a 6 month period" in postings. Otherwise: 3 in 30 days by default, Google 3 in 90 days, and any company caps set under You, Rules. |
| Unrelated roles at one company (`role_spread`) | Ask | This job would make more than 2 kinds of role active at the company. Kinds are read from the title: database, data, ML, infrastructure, or software for everything else. |
| Pace per company (`same_day`) | Warn | Two or more applications to the company were approved or sent today. |
| Pace per application system (`ats_daily`) | Warn | 25 or more applications went out today on the same system (Greenhouse, Lever or Ashby). Bursts from one email can get flagged as automated. |

### Cooldowns and ongoing processes

| Rule | Default | Breaks when |
|---|---|---|
| Reapplying after a rejection (`cooldown`) | Block | I was rejected at the company for the same kind of role within the cooldown (180 days by default), and that job had reached interviews. After a rejection without interviews (a resume screen), it asks instead of blocking. The agent knows a job reached interviews when I mark it Interviewing or the inbox sees an interview request. |
| Already interviewing there (`interviewing`) | Ask | Another job at the company is in interviews. Better to ask the recruiter about this role. |
| Withdrew or declined there (`withdrew`) | Ask | I marked a job at the company Withdrew in the last 180 days. |

### Eligibility

| Rule | Default | Breaks when |
|---|---|---|
| Graduation window (`grad_window`) | Block | The title, posting or form names graduation dates my graduation isn't in, next to words like new grad, graduating, class of, emerging talent, early career, university grad or campus hire ("Applied Emerging Talent (2027)" is for 2027 graduates). Months count: "Fall 2026 or Spring 2027" means December 2026 to May 2027, so a May 2026 graduate is outside it. A year on its own covers the whole year. My graduation comes from the profile, or else from the resume's first degree. |
| Citizenship or clearance (`citizenship`) | Block | The posting requires U.S. citizenship, a security clearance or ITAR, or the posting or form says you must be a U.S. person (export control) and my profile says I'm not one. Most of these postings are already dropped by the search's filters; this catches the rest. |
| No sponsorship (`no_sponsor`) | Ask | The posting says it won't sponsor visas and my profile says I'll need sponsorship. |
| Experience well above mine (`years`) | Warn | The posting asks for more than 2 years beyond the experience in my profile. |
| Doctorate required (`phd`) | Warn | The posting requires a PhD and my profile's degree isn't a doctorate. |
| On-site I can't accept (`onsite`) | Ask | The job isn't remote or in New York, the posting or form asks for office days or relocation, and my profile says I won't relocate. |

### Consistency within one company

A company's application system keeps all my applications on one candidate record, so recruiters see them side by side.

| Rule | Default | Breaks when |
|---|---|---|
| Different answers to one company (`consistency`) | Ask | My answer on salary, start date, relocation, sponsorship, working on-site or work authorization differs from the one on another approved, applied or interviewing application at the company. |
| Different resumes to one company (`resume_consistency`) | Warn | Another application at the company in the last 60 days used a tailored resume with a different summary or different projects. |

### Company policies and conflicts

| Rule | Default | Breaks when |
|---|---|---|
| AI-use policy (`ai_policy`) | Ask | A form question mentions an AI policy, AI assistants, AI tools, ChatGPT or artificial intelligence, or the posting asks applicants not to use AI. Until I approve it anyway, the autofill leaves out everything the model wrote: drafted answers stay for me, my usual resume goes in place of the tailored one, and no cover letter is attached. |
| Referral or agency on file (`referral`) | Block | I noted a referral or an agency submission for the company under You, Rules. Applying directly can create an ownership conflict or cost the referral. |
| Conflict answers (`conflicts`) | Ask | The form asks about a non-compete, government employment or ties to a government official, and my profile answers Yes. |

## Already handled elsewhere

- Consents and attestations each need my own tick on the review card, because some of them are factual claims, like a graduation date.
- Questions like "Do you possess 2 years of experience in X?" always come to me, never to a profile field.
- The tailored summary is dropped if it claims a skill my resume doesn't list, and the cover letter gets a warning if it does.

## Settings

Under You, Rules:

- The action for each rule.
- The default company cap (3 applications in 30 days) and caps for specific companies (Google starts at 3 in 90).
- The cooldown after a rejection that followed interviews (180 days).
- Referral and agency notes, one per company.

## Where the numbers come from

- Google has capped candidates at 3 applications per rolling window; reports differ on whether it's 30 or 90 days ([Blind](https://www.teamblind.com/post/Why-does-Google-still-limit-candidates-to-only-three-applications-in-30-days-dqXExLYL), [Glassdoor](https://www.glassdoor.com/Community/exit-opportunities-3/is-there-a-monthly-limit-to-the-number-of-jobs-you-can-apply-at-google)).
- Big tech companies commonly enforce 6 to 12 month cooldowns after interview rejections, with little or none after resume screens ([Leon Consulting](https://leonstaff.com/blogs/big-tech-interview-cooldown-periods-guide/), [Apt](https://www.tryapt.ai/blog/when-to-reapply-after-job-rejection)).
- Anthropic asked applicants not to use AI assistants in applications, and later changed its policy; its forms still include an AI policy question ([Fortune](https://fortune.com/2025/02/04/anthropic-tells-job-candidates-dont-use-ai-employer-trend), [Inc.](https://www.inc.com/chris-morris/why-anthropic-changed-policy-banning-ai-job-applicants/91219358)).
- The graduation window came from Scale AI's form, which asks applicants to confirm a Fall 2026 or Spring 2027 graduation.

Companies change these policies. When a company states its own rule, add it as a company cap or change the rule's action.
