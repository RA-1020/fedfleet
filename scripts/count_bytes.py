"""Print how many bytes travel per update: full model vs task head (fp32 and int8)."""

from flwr.app import ArrayRecord

from fedfleet.model import build_model, count_params

model = build_model(11)
full = ArrayRecord(torch_state_dict=model.state_dict()).count_bytes()
head = ArrayRecord(torch_state_dict=model.head.state_dict()).count_bytes()
print(f"full model : {count_params(model):>9,} params  {full:>11,} bytes per update")
print(f"task head  : {count_params(model.head):>9,} params  {head:>11,} bytes per update")
print(f"head, int8 : {count_params(model.head):>9,} params  ~{count_params(model.head):>10,} bytes (Layer 2 estimate)")
print(f"reduction  : {100 * (1 - head / full):.2f}%")
