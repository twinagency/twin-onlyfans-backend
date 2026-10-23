# Claude release ops, backend beaf89d + dashboard c75e101

- Image: 323149985112.dkr.ecr.ap-southeast-2.amazonaws.com/twin-onlyfans@sha256:0dfd6a2663fbf4a1d29964c3c4c24a72e9aae72382431bef3047bbfc1fd2a04e
  (CodeBuild fbc2eab5-341f-4f94-8330-465f63576eea, OCI revision label verified = beaf89d4aff021eac19b638b99d85a9d13df306e)
- Source zip sha256 95a515a6e099cccc872101a03fab7b1a9655f0b244896055fb07648cb8c2e1dc
- IAM: added ReadReleaseSourcebeaf89d4 (GetObject on that zip only, expires 2026-10-24T13:55:20Z); removed ReadReleaseSource14bc99eb
- Acceptance runs: 39963731137 (combined), 39963869733 (offer import + content sync)
- deploy-native-beaf89d.py: handoff deploy-native-reviewed.py with BACKEND_SHA -> beaf89d. Staged on host at /root/of-release-beaf89d/deploy.py (sha256 bd0785b2448956f7db8f7548d86811bd129a661437899b3e63c6aa1ca3c7f3b5)

## Rollback
- Before migration fails: previous compose saved at /etc/twin-onlyfans/native-release-<stamp>/previous-compose.json; `docker compose -p twin-onlyfans -f <that> up -d --no-build --no-deps api worker` restores old image 7e23784f.
- After migration (schema 0091, downgrade raises): restore encrypted backup
  /var/backups/twin-onlyfans/final-0088-20261023T122645Z-9739cd/checkpoint.twinof
  (sha256 4875ff84a98438e932c0fa472d4a8cc19be2bea862ef991a1781c6a99abaa43e, key /etc/twin-onlyfans/backup-key-0088-20261023T122645Z-9739cd, restore tested) then previous compose.
