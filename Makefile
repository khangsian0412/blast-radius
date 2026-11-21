PYTHON ?= python3
IMAGE ?= blast-radius
SOURCE ?= baseline
DATA ?= sample

.PHONY: test build sample check local baseline

test:
	$(PYTHON) -m unittest discover -s tests -v

build:
	docker build --platform linux/amd64 -t $(IMAGE) .

sample: build
	docker run --rm --platform linux/amd64 --network none --read-only --cpus 2 --memory 4g --pids-limit 256 --tmpfs /tmp:rw,nosuid,nodev,size=64m --user 65534:65534 -e HOME=/tmp -v "$(abspath $(DATA)/commits):/data/commits:ro" -v "$(abspath $(DATA)/tests.json):/data/tests.json:ro" $(IMAGE) > output.jsonl
	$(PYTHON) tools/score.py $(DATA) $(DATA)/answers.json output.jsonl

baseline:
	$(PYTHON) tools/fetch_baseline.py $(SOURCE)

local:
	$(PYTHON) selector.py --data $(DATA) --source $(SOURCE) > output.jsonl
	$(PYTHON) tools/score.py $(DATA) $(DATA)/answers.json output.jsonl

check:
	$(PYTHON) tools/validate.py $(DATA) output.jsonl
