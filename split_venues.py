import json

with open('venues/districtvenues.json', encoding='utf-8') as f:
    venues = json.load(f)

total = len(venues)
n_parts = 6
base = total // n_parts
rem = total % n_parts
sizes = [base + (1 if i < rem else 0) for i in range(n_parts)]

idx = 0
for i, size in enumerate(sizes):
    shard_num = 9 + i
    chunk = venues[idx: idx + size]
    idx += size
    with open(f'venues/venues{shard_num}.json', 'w', encoding='utf-8') as f:
        json.dump(chunk, f, ensure_ascii=False, indent=2)
    print(f'venues{shard_num}: {len(chunk)} venues')

print('total:', total)