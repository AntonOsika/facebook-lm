import json
import pandas as pd

ME_START_CHAR = "\u001C"
FRIEND_START_CHAR = "\u001D"

def load_from_json(file, chunk_length=3):
    with open(file) as f:
        data = json.load(f)
        me = data["user"]
        assert len(data["threads"][0]["participants"]) == 1, "Please use a conversation between two persons only"
        conversation = data["threads"][0]["messages"]

    chunks = []
    current_chunk = []
    for message in conversation:
        if message["sender"] == me:
            current_chunk.append(ME_START_CHAR + message["message"])
        else:
            current_chunk.append(FRIEND_START_CHAR + message["message"])

        # Include all overlapping pairs of two:
        if len(current_chunk) > 1:
            chunks.append("".join(current_chunk[-2:]))
        
        if len(current_chunk) >= chunk_length:
            chunks.append("".join(current_chunk))
            current_chunk = current_chunk[-1:]  # Changed to include overlapping chunks

    df = pd.DataFrame(chunks, columns=["message_chunk"])
    df["len"] = df["message_chunk"].apply(lambda x: len(x))  # Process shorter chunks first
    df = df[df["len"] < 2000]  # Only process chunks shorter than 2000 chars, otherwise memory blows up
    df.sort_values(by="len", ascending=True, inplace=True)
    print("Num chunks: {}".format(len(df)))
    return df
