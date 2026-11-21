"""Merge independent, patch-free baseline measurements as plain JSON."""

import hashlib
import json
from pathlib import Path
import sys


def main():
    target = Path(sys.argv[1])
    data = json.loads(target.read_text())
    pool = [int(value, 16) for value in data["pool"]]
    pool_index = {bits: i for i, bits in enumerate(pool)}
    test_index = {name: i for i, name in enumerate(data["tests"])}
    if not data.get("outside_class_pool_valid"):
        # Older collectors interned these masks after serializing their pool.
        # All-context masks safely upper-bound the outside-task subsets, even
        # when later profile merges happened to reuse an invalid forward index.
        data["classes_outside"] = dict(data.get("classes", {}))
        data["attributes_outside"] = dict(data.get("attributes", {}))
        data["outside_class_pool_valid"] = True
        data["outside_class_masks_conservative"] = True

    def intern(bits):
        if bits not in pool_index:
            pool_index[bits] = len(pool)
            pool.append(bits)
        return pool_index[bits]

    for name in sys.argv[2:]:
        extra = json.loads(Path(name).read_text())
        def readers(names):
            return sum(1 << test_index[nodeid] for nodeid in set(names) if nodeid in test_index)

        if "shared_lines" in extra:
            for file, lines in extra.pop("shared_lines").items():
                record = data["files"].setdefault(file, {"lines": {}})
                for line, names in lines.items():
                    bits = readers(names)
                    for table in (record["lines"], record.setdefault("outside", {})):
                        table[line] = intern(pool[table.get(line, 0)] | bits)
        for source_key, targets in (
            ("shared_fixtures", ("fixtures",)),
            ("shared_source_reads", ("source_reads",)),
            ("shared_classes", ("classes", "classes_outside")),
            ("shared_attributes", ("attributes", "attributes_outside")),
        ):
            for key, names in extra.pop(source_key, {}).items():
                bits = readers(names)
                for target_key in targets:
                    table = data.setdefault(target_key, {})
                    table[key] = intern(pool[table.get(key, 0)] | bits)
        if "source_reads" in extra:
            data["source_reads"] = {
                file: intern(sum(1 << test_index[nodeid] for nodeid in set(names) if nodeid in test_index))
                for file, names in extra.pop("source_reads").items()
            }
        if "import_lines" in extra:
            for file, lines in extra.pop("import_lines").items():
                if file in data["files"]:
                    data["files"][file]["init"] = lines
        if "child_lines" in extra:
            child_lines = extra.pop("child_lines")
            data["subprocess_lines"] = {
                file: sorted(map(int, lines)) for file, lines in child_lines.items()
            }
            for file, lines in child_lines.items():
                record = data["files"].setdefault(file, {"lines": {}})
                for line, names in lines.items():
                    bits = 0
                    for nodeid in names:
                        if nodeid in test_index:
                            bits |= 1 << test_index[nodeid]
                    record["lines"][line] = intern(
                        pool[record["lines"].get(line, 0)] | bits
                    )
                    outside = record.setdefault("outside", {})
                    outside[line] = intern(pool[outside.get(line, 0)] | bits)
            always = pool[data.get("always", 0)]
            for nodeid in extra.get("traced_children", ()):
                if nodeid in test_index:
                    always &= ~(1 << test_index[nodeid])
            data["always"] = intern(always)
        if "dictionary_reads" in extra:
            dictionaries = {}
            for origin, record in extra.pop("dictionary_reads").items():
                if record["unsafe"]:
                    continue
                reads = {}
                for key, names in record["reads"].items():
                    bits = 0
                    for nodeid in names:
                        if nodeid in test_index:
                            bits |= 1 << test_index[nodeid]
                    reads[key] = intern(bits)
                dictionaries[origin] = {**record, "reads": reads}
            data["dictionaries"] = dictionaries
        if 'import_attributes' in extra:
            data['attribute_init'] = sorted(set(data.get('attribute_init', ())) | set(extra.pop('import_attributes')))
        data.update(extra)
    data["pool"] = [format(bits, "x") for bits in pool]
    target.write_text(json.dumps(data, separators=(",", ":")) + "\n")
    print(
        f"Merged baseline map: {target.stat().st_size:,} bytes; sha256 {hashlib.sha256(target.read_bytes()).hexdigest()}"
    )


if __name__ == "__main__":
    main()
