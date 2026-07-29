from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretRule:
    id: str
    source: str
    flags: int = 0


@dataclass(frozen=True)
class SecretMatch:
    rule_id: str
    label: str


_ANT_KEY_PFX = "-".join(("sk", "ant", "api"))

_SECRET_RULES: tuple[SecretRule, ...] = (
    SecretRule("aws-access-token", r"\b((?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16})\b"),
    SecretRule("gcp-api-key", r"\b(AIza[\w-]{35})(?:[\x60'\"\s;]|\\[nr]|$)"),
    SecretRule(
        "azure-ad-client-secret",
        r"(?:^|[\\'\"\x60\s>=:(,)])([a-zA-Z0-9_~.]{3}\dQ~[a-zA-Z0-9_~.-]{31,34})(?:$|[\\'\"\x60\s<),])",
    ),
    SecretRule("digitalocean-pat", r"\b(dop_v1_[a-f0-9]{64})(?:[\x60'\"\s;]|\\[nr]|$)"),
    SecretRule(
        "digitalocean-access-token",
        r"\b(doo_v1_[a-f0-9]{64})(?:[\x60'\"\s;]|\\[nr]|$)",
    ),
    SecretRule(
        "anthropic-api-key",
        rf"\b({_ANT_KEY_PFX}03-[a-zA-Z0-9_\-]{{93}}AA)(?:[\x60'\"\s;]|\\[nr]|$)",
    ),
    SecretRule(
        "anthropic-admin-api-key",
        r"\b(sk-ant-admin01-[a-zA-Z0-9_\-]{93}AA)(?:[\x60'\"\s;]|\\[nr]|$)",
    ),
    SecretRule(
        "openai-api-key",
        r"\b(sk-(?:proj|svcacct|admin)-(?:[A-Za-z0-9_-]{74}|[A-Za-z0-9_-]{58})T3BlbkFJ(?:[A-Za-z0-9_-]{74}|[A-Za-z0-9_-]{58})\b|sk-[a-zA-Z0-9]{20}T3BlbkFJ[a-zA-Z0-9]{20})(?:[\x60'\"\s;]|\\[nr]|$)",
    ),
    SecretRule("huggingface-access-token", r"\b(hf_[a-zA-Z]{34})(?:[\x60'\"\s;]|\\[nr]|$)"),
    SecretRule("github-pat", r"ghp_[0-9a-zA-Z]{36}"),
    SecretRule("github-fine-grained-pat", r"github_pat_\w{82}"),
    SecretRule("github-app-token", r"(?:ghu|ghs)_[0-9a-zA-Z]{36}"),
    SecretRule("github-oauth", r"gho_[0-9a-zA-Z]{36}"),
    SecretRule("github-refresh-token", r"ghr_[0-9a-zA-Z]{36}"),
    SecretRule("gitlab-pat", r"glpat-[\w-]{20}"),
    SecretRule("gitlab-deploy-token", r"gldt-[0-9a-zA-Z_\-]{20}"),
    SecretRule("slack-bot-token", r"xoxb-[0-9]{10,13}-[0-9]{10,13}[a-zA-Z0-9-]*"),
    SecretRule("slack-user-token", r"xox[pe](?:-[0-9]{10,13}){3}-[a-zA-Z0-9-]{28,34}"),
    SecretRule("slack-app-token", r"xapp-\d-[A-Z0-9]+-\d+-[a-z0-9]+", flags=re.IGNORECASE),
    SecretRule("twilio-api-key", r"SK[0-9a-fA-F]{32}"),
    SecretRule(
        "sendgrid-api-token",
        r"\b(SG\.[a-zA-Z0-9=_\-.]{66})(?:[\x60'\"\s;]|\\[nr]|$)",
    ),
    SecretRule("npm-access-token", r"\b(npm_[a-zA-Z0-9]{36})(?:[\x60'\"\s;]|\\[nr]|$)"),
    SecretRule("pypi-upload-token", r"pypi-AgEIcHlwaS5vcmc[\w-]{50,1000}"),
    SecretRule("databricks-api-token", r"\b(dapi[a-f0-9]{32}(?:-\d)?)(?:[\x60'\"\s;]|\\[nr]|$)"),
    SecretRule("hashicorp-tf-api-token", r"[a-zA-Z0-9]{14}\.atlasv1\.[a-zA-Z0-9\-_=]{60,70}"),
    SecretRule("pulumi-api-token", r"\b(pul-[a-f0-9]{40})(?:[\x60'\"\s;]|\\[nr]|$)"),
    SecretRule(
        "postman-api-token",
        r"\b(PMAK-[a-fA-F0-9]{24}-[a-fA-F0-9]{34})(?:[\x60'\"\s;]|\\[nr]|$)",
    ),
    SecretRule(
        "grafana-api-key",
        r"\b(eyJrIjoi[A-Za-z0-9+/]{70,400}={0,3})(?:[\x60'\"\s;]|\\[nr]|$)",
    ),
    SecretRule(
        "grafana-cloud-api-token",
        r"\b(glc_[A-Za-z0-9+/]{32,400}={0,3})(?:[\x60'\"\s;]|\\[nr]|$)",
    ),
    SecretRule(
        "grafana-service-account-token",
        r"\b(glsa_[A-Za-z0-9]{32}_[A-Fa-f0-9]{8})(?:[\x60'\"\s;]|\\[nr]|$)",
    ),
    SecretRule("sentry-user-token", r"\b(sntryu_[a-f0-9]{64})(?:[\x60'\"\s;]|\\[nr]|$)"),
    SecretRule(
        "sentry-org-token",
        r"\bsntrys_eyJpYXQiO[a-zA-Z0-9+/]{10,200}(?:LCJyZWdpb25fdXJs|InJlZ2lvbl91cmwi|cmVnaW9uX3VybCI6)[a-zA-Z0-9+/]{10,200}={0,2}_[a-zA-Z0-9+/]{43}",
    ),
    SecretRule(
        "stripe-access-token",
        r"\b((?:sk|rk)_(?:test|live|prod)_[a-zA-Z0-9]{10,99})(?:[\x60'\"\s;]|\\[nr]|$)",
    ),
    SecretRule("shopify-access-token", r"shpat_[a-fA-F0-9]{32}"),
    SecretRule("shopify-shared-secret", r"shpss_[a-fA-F0-9]{32}"),
    SecretRule(
        "private-key",
        r"-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----[\s\S-]{64,}?-----END[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----",
        flags=re.IGNORECASE,
    ),
)

_COMPILED_RULES = tuple(
    (rule.id, re.compile(rule.source, rule.flags))
    for rule in _SECRET_RULES
)

_LABEL_OVERRIDES = {
    "aws-access-token": "AWS Access Token",
    "gcp-api-key": "GCP API Key",
    "azure-ad-client-secret": "Azure AD Client Secret",
    "digitalocean-pat": "DigitalOcean PAT",
    "digitalocean-access-token": "DigitalOcean Access Token",
    "anthropic-api-key": "Anthropic API Key",
    "anthropic-admin-api-key": "Anthropic Admin API Key",
    "openai-api-key": "OpenAI API Key",
    "huggingface-access-token": "Hugging Face Access Token",
    "github-pat": "GitHub PAT",
    "github-fine-grained-pat": "GitHub Fine-Grained PAT",
    "github-app-token": "GitHub App Token",
    "github-oauth": "GitHub OAuth Token",
    "github-refresh-token": "GitHub Refresh Token",
    "gitlab-pat": "GitLab PAT",
    "gitlab-deploy-token": "GitLab Deploy Token",
    "slack-bot-token": "Slack Bot Token",
    "slack-user-token": "Slack User Token",
    "slack-app-token": "Slack App Token",
    "twilio-api-key": "Twilio API Key",
    "sendgrid-api-token": "SendGrid API Token",
    "npm-access-token": "NPM Access Token",
    "pypi-upload-token": "PyPI Upload Token",
    "databricks-api-token": "Databricks API Token",
    "hashicorp-tf-api-token": "HashiCorp TF API Token",
    "pulumi-api-token": "Pulumi API Token",
    "postman-api-token": "Postman API Token",
    "grafana-api-key": "Grafana API Key",
    "grafana-cloud-api-token": "Grafana Cloud API Token",
    "grafana-service-account-token": "Grafana Service Account Token",
    "sentry-user-token": "Sentry User Token",
    "sentry-org-token": "Sentry Org Token",
    "stripe-access-token": "Stripe Access Token",
    "shopify-access-token": "Shopify Access Token",
    "shopify-shared-secret": "Shopify Shared Secret",
    "private-key": "Private Key",
}


def _rule_id_to_label(rule_id: str) -> str:
    override = _LABEL_OVERRIDES.get(rule_id)
    if override is not None:
        return override
    return " ".join(part.capitalize() for part in rule_id.split("-"))


def scan_for_secrets(content: str) -> tuple[SecretMatch, ...]:
    matches: list[SecretMatch] = []
    seen_rule_ids: set[str] = set()
    for rule_id, pattern in _COMPILED_RULES:
        if rule_id in seen_rule_ids:
            continue
        if pattern.search(content) is None:
            continue
        seen_rule_ids.add(rule_id)
        matches.append(SecretMatch(rule_id=rule_id, label=_rule_id_to_label(rule_id)))
    return tuple(matches)
