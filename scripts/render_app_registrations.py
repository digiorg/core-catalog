#!/usr/bin/env python3
"""Build-time GitOps adapter, never a cluster writer.

Reads the canonical Composition; emits only dashboard ConfigMaps and inventory
rules. No Application, workload, namespace or telemetry deletion is emitted.
Use a verified canonical Catalog checkout and pinned KCL 0.12.7 executable.
"""
import argparse
import copy
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import yaml

ROOT = Path(__file__).resolve().parents[1]


def validate(registrations, snapshot):
    if not isinstance(registrations, list) or not isinstance(snapshot, dict) or any(
            not isinstance(snapshot.get(k), list) for k in ('appClaims', 'namespaces', 'configMaps', 'grafanaDashboards')):
        raise ValueError('complete secret-free collision snapshot required')
    seen = set()
    for e in registrations:
        if not isinstance(e, dict) or set(e) != {'contractVersion', 'appId', 'namespace', 'ownerTeam', 'dedicatedNamespace', 'dashboardDesired', 'expectedWorkloads'}:
            raise ValueError('unknown/missing registration fields')
        app = e['appId']
        if not isinstance(app, str) or not re.fullmatch(r'[a-z]([a-z0-9-]{0,30}[a-z0-9])?', app) or app in seen:
            raise ValueError('unsafe or duplicate app identity')
        seen.add(app)
        if app != e['namespace'] or app in {'default', 'monitoring', 'argocd', 'kyverno', 'crossplane-system'} or app.startswith('kube-'):
            raise ValueError('dedicated non-system namespace must equal app ID')
        if type(e['contractVersion']) is not int or e['contractVersion'] != 1 or e['dedicatedNamespace'] is not True or type(e['dashboardDesired']) is not bool:
            raise ValueError('explicit v1 dedicated-namespace/dashboard contract required')
        if not isinstance(e['ownerTeam'], str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', e['ownerTeam']):
            raise ValueError('invalid ownerTeam')
        workloads = e['expectedWorkloads']
        if not isinstance(workloads, list):
            raise ValueError('expectedWorkloads must be explicit list')
        identities = set()
        for w in workloads:
            if not isinstance(w, dict) or set(w) != {'kind', 'name'} or w['kind'] not in ('Deployment', 'StatefulSet', 'DaemonSet') or not isinstance(w['name'], str) or not re.fullmatch(r'[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?', w['name']):
                raise ValueError('unsupported/unsafe expected workload')
            identity = (w['kind'], w['name'])
            if identity in identities:
                raise ValueError('duplicate expected workload')
            identities.add(identity)
        if any(c.get('spec', {}).get('appName') == app for c in snapshot['appClaims']):
            raise ValueError('Claim and registration cannot share app/namespace')
        namespaces = [n for n in snapshot['namespaces'] if n.get('metadata', {}).get('name') == app]
        if len(namespaces) > 1 or any(n.get('metadata', {}).get('labels', {}).get('platform.digiorg.io/app-id', app) != app or n.get('metadata', {}).get('labels', {}).get('platform.digiorg.io/type', 'app') != 'app' for n in namespaces):
            raise ValueError('ambiguous/system namespace ownership')
        existing = [c for c in snapshot['configMaps'] if c.get('metadata', {}).get('name') == 'grafana-app-dev-' + app and c.get('metadata', {}).get('namespace') == app]
        if len(existing) > 1:
            raise ValueError('ambiguous dashboard target')
        for cm in existing:
            ann = cm['metadata'].get('annotations', {})
            if ann.get('platform.digiorg.io/dashboard-owner') != 'gitops:' + app or ann.get('platform.digiorg.io/app-id') != app or ann.get('grafana_folder') != '/tmp/dashboards/DigiOrg Apps' or cm['metadata'].get('labels', {}).get('grafana_dashboard') != '1':
                raise ValueError('foreign/manual target cannot be adopted')
        for d in snapshot['grafanaDashboards']:
            if d.get('uid') == 'app-dev-' + app and (not existing or d.get('owner') != 'gitops:' + app):
                raise ValueError('foreign/manual Grafana UID collision')


def generate(registrations, snapshot, render):
    validate(registrations, snapshot)
    docs, rules = [], []
    for entry in sorted(registrations, key=lambda e: e['appId']):
        app = entry['appId']
        uid = 'gitops:' + app
        xr = {'apiVersion': 'platform.digiorg.io/v1alpha1', 'kind': 'Application',
              'metadata': {'name': app, 'uid': uid, 'generation': 1}, 'status': {},
              'spec': {'appName': app, 'team': entry['ownerTeam'], 'size': 'S', 'services': [],
                       'database': {'enabled': False}, 'gitea': {'enabled': False}, 'messaging': {'enabled': False}}}
        params = {'oxr': xr, 'requiredResources': {'dashboardApplications': [{'Resource': xr}],
                  'targetNamespace': [{'Resource': n} for n in snapshot['namespaces'] if n['metadata']['name'] == app]}}
        items = render(params)
        if entry['dashboardDesired']:
            cms = [i['spec']['forProvider']['manifest'] for i in items
                   if i.get('metadata', {}).get('annotations', {}).get('krm.kcl.dev/composition-resource-name') == 'grafana-dashboard']
            docs.extend(copy.deepcopy(cms))
        labels = {'app_id': app, 'namespace': entry['namespace'], 'claim_uid': uid, 'owner_team': entry['ownerTeam']}
        rules.append({'record': 'digiorg_registered_app_info', 'expr': 'vector(1)', 'labels': labels})
        for workload in sorted(entry['expectedWorkloads'], key=lambda w: (w['kind'], w['name'])):
            rules.append({'record': 'digiorg_registered_app_service_info', 'expr': 'vector(1)',
                          'labels': {'app_id': app, 'namespace': app, 'kind': workload['kind'], 'service': workload['name']}})
    if rules:
        docs.append({'apiVersion': 'monitoring.coreos.com/v1', 'kind': 'PrometheusRule',
                     'metadata': {'name': 'digiorg-registered-apps', 'namespace': 'monitoring', 'labels': {'release': 'prometheus'}},
                     'spec': {'groups': [{'name': 'digiorg.registered-apps', 'interval': '30s', 'rules': rules}]}})
    return docs


def render_canonical(params, kcl):
    doc = yaml.safe_load((ROOT / 'compositions/local/pipeline.yaml').read_text())
    source = next(s for s in doc['spec']['pipeline'] if s['functionRef']['name'] == 'function-kcl')['input']['spec']['source']
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / 'main.k'
        path.write_text(source)
        result = subprocess.run([kcl, 'run', str(path), '-D', 'params=' + json.dumps(params)], capture_output=True, text=True, timeout=60, check=True)
        return yaml.safe_load(result.stdout)['items']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registrations', required=True)
    parser.add_argument('--snapshot', required=True, help='Secret-free read-only collision inventory: appClaims/namespaces/configMaps/grafanaDashboards')
    parser.add_argument('--kcl', required=True, help='Explicit verified KCL0.12.7 executable; no implicit download')
    args = parser.parse_args()
    entries = yaml.safe_load(Path(args.registrations).read_text())['apps']
    snapshot = json.loads(Path(args.snapshot).read_text())
    docs = generate(entries, snapshot, lambda p: render_canonical(p, args.kcl))
    print(yaml.safe_dump_all(docs, sort_keys=False), end='')


if __name__ == '__main__':
    main()
