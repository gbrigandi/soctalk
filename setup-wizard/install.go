package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/google/uuid"
)

// installWorker consumes installRequest from state.installCh. v3 design
// (codex feedback): the wizard does NOT run helm. It writes config
// files + a sentinel, then exits. soctalk-firstboot.service (the
// existing installer) then runs the helm install with the supplied
// values. This keeps the wizard a thin "values/secret generator" and
// reuses the install path that already works for the cloud-init case.
func installWorker(s *serverState) {
	for req := range s.installCh {
		s.updateStatus("running", "writing values file", "", "")
		if err := writeConfig(s, req); err != nil {
			s.updateStatus("error", "failed to write config files", "", err.Error())
			continue
		}

		// Drop the wizard's done-sentinel — firstboot.service polls
		// for it (or for cloud-init's values.yaml) before starting.
		if err := os.WriteFile(s.wizardSentinelPath, []byte(time.Now().UTC().Format(time.RFC3339)+"\n"), 0o644); err != nil {
			s.updateStatus("error", "wrote config but failed to mark wizard sentinel", "", err.Error())
			continue
		}

		// Tell the customer where to go. We can't easily wait for
		// helm install to finish here without re-implementing the
		// installer; instead we hand them the UI URL plus a hint
		// that boot is still progressing.
		ui := uiURL(req.Hostname)
		s.updateStatus("success",
			"config saved. Installer is running in the background — give it ~3 minutes, then open the SocTalk URL.",
			ui, "")

		// Give the customer's poller time to grab the success
		// response, then exit. systemd will not re-fire because of
		// ConditionPathExists=!sentinel.
		go func() {
			time.Sleep(3 * time.Second)
			os.Exit(0)
		}()
	}
}

func writeConfig(s *serverState, req installRequest) error {
	if err := os.MkdirAll(filepath.Dir(s.valuesPath), 0o755); err != nil {
		return err
	}
	values, err := renderValues(req)
	if err != nil {
		return fmt.Errorf("render values: %w", err)
	}
	// 0640 root:root — installer (running as root) reads, world cannot.
	if err := os.WriteFile(s.valuesPath, []byte(values), 0o640); err != nil {
		return fmt.Errorf("write values: %w", err)
	}
	if err := os.WriteFile(s.llmKeyPath, []byte(req.LLMAPIKey+"\n"), 0o600); err != nil {
		return fmt.Errorf("write llm.key: %w", err)
	}
	// Drop a shell-sourced env file with what firstboot.sh needs for the
	// post-helm-install demo tenant onboarding step. Mirrors poc-funnel's
	// cloud-init pattern. Slug is hardcoded "demo" — matches the demo
	// box's seeded tenant.
	host := req.Hostname
	if host == "" {
		host = "soctalk.local"
	}
	tenantName := req.MSSPName + " — Demo"
	envContents := fmt.Sprintf(
		"ADMIN_EMAIL=%s\nADMIN_PW=%s\nINGRESS_HOST=%s\nTENANT_SLUG=demo\nTENANT_NAME=%s\n",
		shellQuote(req.AdminEmail),
		shellQuote(req.AdminPW),
		shellQuote(host),
		shellQuote(tenantName),
	)
	if err := os.WriteFile(filepath.Join(filepath.Dir(s.valuesPath), "onboard.env"), []byte(envContents), 0o600); err != nil {
		return fmt.Errorf("write onboard.env: %w", err)
	}
	return nil
}

// shellQuote returns the string single-quoted, with embedded single
// quotes escaped via the standard '\'' pattern. Safe for "VAR=$value"
// shell-source files.
func shellQuote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'"
}

func renderValues(req installRequest) (string, error) {
	msspID := uuid.New().String()
	installID := uuid.New().String()
	// Ingress hostnames MUST be DNS names (k8s rejects IP addresses on
	// spec.rules[0].host). When the customer doesn't supply a hostname,
	// use a placeholder DNS name for the Ingress. The auth layer derives
	// the CSRF-acceptable Origin from ingress.hostnames, so the
	// in-cluster onboarding curl (which sends Host: soctalk.local) and
	// a browser that maps soctalk.local in /etc/hosts both pass the
	// Origin check. We deliberately do NOT set auth.publicOriginOverride —
	// setting it to the IP would block the legitimate soctalk.local
	// Origin, and setting it to soctalk.local would be redundant.
	host := req.Hostname
	if host == "" {
		host = "soctalk.local"
	}

	// The installer reads /etc/soctalk/llm.key separately and creates the
	// Secret from it (no secrets in helm release history). firstboot.sh
	// materializes that one key into BOTH the anthropic-api-key and
	// openai-api-key sub-keys; the runtime then picks the provider, model,
	// AND which sub-key to read from defaults.llm.provider (see the
	// soctalk-system chart's 30-api.yaml). So the provider the operator
	// selected here MUST be written into values.yaml — otherwise it stays
	// at the chart default (openai) and an Anthropic key is sent to
	// api.openai.com, returning 401 and silently failing every triage.
	// Map the choice to a provider-appropriate baseUrl + a sane default
	// model (mirrors deploy/demo-values.yaml's working pattern).
	llmBaseURL, llmModel := "https://api.openai.com/v1", "gpt-4o"
	if req.LLMProvider == "anthropic" {
		llmBaseURL, llmModel = "https://api.anthropic.com", "claude-sonnet-4-6"
	}

	// NO `image:` override here on purpose. This used to emit
	// `image: {registry: ghcr.io/soctalk, tag: "latest"}`, which meant a
	// released appliance (e.g. soctalk-demo-0.2.1) installed whatever
	// `latest` pointed at — main's HEAD — so an artifact labelled with a
	// version ran unreleased code, and its behaviour changed over time
	// without the artifact changing. Omitting the override lets the BUNDLED
	// chart's own default apply, which is that chart's version. That keeps
	// the appliance pinned to the release it shipped with and, unlike a
	// hardcoded version string, cannot drift out of sync on the next cut.
	return fmt.Sprintf(`# Generated by soctalk-setup-wizard at %s. Do not hand-edit.
install:
  msspId: %q
  msspName: %q
  installId: %q
  installLabel: "demo"
  bootstrapAdmin:
    email: %q
    password: %q
ingress:
  enabled: true
  className: traefik
  tls:
    issuerRef: ""
    secretName: soctalk-tls
  hostnames:
    mssp: %q
    customer: %q
postgres:
  enabled: true
  storage:
    size: 20Gi
preInstallCheck:
  enabled: false
defaults:
  llm:
    provider: %q
    baseUrl: %q
    model: %q
`,
		time.Now().UTC().Format(time.RFC3339),
		msspID, req.MSSPName, installID,
		req.AdminEmail, req.AdminPW,
		host, host,
		req.LLMProvider, llmBaseURL, llmModel,
	), nil
}

func uiURL(hostname string) string {
	if hostname == "" {
		ips := localIPs()
		if len(ips) > 0 {
			return "https://" + ips[0].String() + "/login"
		}
		return "https://soctalk.local/login"
	}
	return "https://" + hostname + "/login"
}
