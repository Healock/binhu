# Binhu environment provisioning

This directory provisions the isolated `development` and `staging` projects.
It never reuses production volumes and never accepts a production database as a
runtime target. Run `python provision.py preview --environment <development|staging>`
before `apply`; `apply` requires an explicit server maintenance window.

The development profile is the Kafka/Redis/Flink architecture integration
baseline. Staging is the sanitized production-copy and load-test target. Old
shadow/load-test projects are evidence only and are not renamed in place.
