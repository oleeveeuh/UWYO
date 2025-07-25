import torch
import torch.nn as nn
import numpy as np
from torch.utils.data import Dataset, DataLoader
from tqdm import tqdm
import pandas as pd
from torch.nn.utils.rnn import pad_sequence
import os
import torch.optim as optim
import random
from sklearn.metrics import accuracy_score, precision_recall_fscore_support
from sklearn.model_selection import train_test_split
import torch.nn.functional as F

seed = 42  # or any integer
torch.manual_seed(seed)
random.seed(seed)
np.random.seed(seed)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
MAX_SEQ_LEN = 5000

def collect_data_and_masks(original_dir, augmented_dir):
    file_pairs = []

    # Original: no masks
    for fname in os.listdir(original_dir):
        if fname.endswith('.txt'):
            data_path = os.path.join(original_dir, fname)
            file_pairs.append((data_path, None))  # no mask

    # Augmented: has masks
    for fname in os.listdir(augmented_dir):
        if fname.startswith('mix_'):
            data_path = os.path.join(augmented_dir, fname)
            mask_path = data_path.replace('mix', 'mask')

            if os.path.exists(mask_path):
                file_pairs.append((data_path, mask_path))
            else:
                print(f"[WARN] Mask not found for {data_path}, skipping.")

    return file_pairs

def spiral_collate_fn(batch):
    sequences, masks, labels = zip(*batch)

    padded_sequences = pad_sequence(sequences, batch_first=True, padding_value=0.0)
    padded_masks = pad_sequence(masks, batch_first=True, padding_value=0)
    labels = torch.stack(labels)

    return padded_sequences, padded_masks, labels

class SpiralDataset(Dataset):
    def __init__(self, file_pairs, label_func=None):
        self.file_pairs = file_pairs
        self.label_func = label_func

    def __len__(self):
        return len(self.file_pairs)

    def __getitem__(self, idx):
        data_path, mask_path = self.file_pairs[idx]

        # Load spiral data: shape [T, 5]
        sequence = np.loadtxt(data_path, delimiter=';')
        sequence = torch.tensor(sequence, dtype=torch.float32)
        sequence = sequence[:MAX_SEQ_LEN]

        # Load or generate mask
        if mask_path is None:
            mask = torch.ones(sequence.shape[0], dtype=torch.bool)  # all valid
        else:
            mask_np = np.loadtxt(mask_path)
            mask = torch.tensor(mask_np, dtype=torch.bool)

        mask = mask[:MAX_SEQ_LEN]
        # Optional: binary label (0=original, 1=augmented)
        if 'Healthy' in data_path:
            label = torch.tensor(0)
        else: label = torch.tensor(1)

        return sequence, mask, label

class PositionalEncoding(nn.Module):
    def __init__(self, d_model, max_len=5000):
        super().__init__()
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len).unsqueeze(1).float()
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-np.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        self.register_buffer('pe', pe.unsqueeze(0))  # shape (1, max_len, d_model)

    def forward(self, x):
        x = x + self.pe[:, :x.size(1)].to(x.device)
        return x

class AttentionPooling(nn.Module):
    def __init__(self, d_model):
        super().__init__()
        self.attn = nn.Linear(d_model, 1)

    def forward(self, x, mask=None):
        """
        x: [B, T, D]
        mask: [B, T] -> 1 for valid, 0 for padding
        """
        logits = self.attn(x).squeeze(-1)  # shape: [batch_size, seq_len]

        # Mask invalid positions with a large negative number
        logits = logits.masked_fill(mask == 0, -1e9)

        # Numerical stability
        logits = logits - logits.max(dim=1, keepdim=True)[0]  # subtract max
        weights = F.softmax(logits, dim=1)                    # shape: [batch_size, seq_len]

        # Apply attention weights
        pooled = torch.bmm(weights.unsqueeze(1), x).squeeze(1)  # shape: [batch_size, d_model]

        # Final safety check (optional)
        pooled = torch.nan_to_num(pooled, nan=0.0, posinf=1e4, neginf=-1e4)
        return pooled

class TabularTransformer(nn.Module):
    def __init__(self, input_dim, d_model=192, nhead=4, num_layers=2, dropout=0.3):
        super().__init__()
        self.input_proj = nn.Linear(input_dim, d_model)
        self.pos_encoder = PositionalEncoding(d_model)

        self.encoder_layers = nn.ModuleList([
            SoftMaskedSelfAttention(d_model, nhead) for _ in range(num_layers)
        ])

        self.pool = AttentionPooling(d_model)

    def forward(self, x, mask=None):
        x = self.input_proj(x)
        x = self.pos_encoder(x)

        for layer in self.encoder_layers:
            x = layer(x, soft_mask=mask)

        pooled = self.pool(x, mask)
        return pooled

class SoftMaskedSelfAttention(nn.Module):
    def __init__(self, d_model, nhead):
        super().__init__()
        self.attn = nn.MultiheadAttention(embed_dim=d_model, num_heads=nhead, batch_first=True)
        self.norm = nn.LayerNorm(d_model)
        self.ff = nn.Sequential(
            nn.Linear(d_model, d_model * 4),
            nn.ReLU(),
            nn.Dropout(0.1),
            nn.Linear(d_model * 4, d_model),
        )

    def forward(self, x, soft_mask=None):
        # x: [B, T, D]
        attn_output, _ = self.attn(x, x, x)  # no hard key_padding_mask

        if soft_mask is not None:
            # Apply soft weights after attention — shape: [B, T, 1]
            soft_mask = soft_mask.unsqueeze(-1)
            attn_output = attn_output * soft_mask  # softly damp irrelevant positions

        x = self.norm(x + attn_output)
        x = self.norm(x + self.ff(x))
        return x
        
class TabularTransformerWithClassifier(nn.Module):
    def __init__(self, encoder, d_model, num_classes):
        super().__init__()
        self.encoder = encoder
        self.classifier = nn.Linear(d_model, num_classes)

    def forward(self, x, mask=None):
        features = self.encoder(x, mask=mask)  # [batch, d_model]
        logits = self.classifier(features)     # [batch, num_classes]
        return logits

def train_epoch(model, dataloader, criterion, optimizer, device):
    model.train()
    losses = []
    all_preds, all_labels = [], []

    for x_batch, mask, y_batch in dataloader:
        x_batch, mask, y_batch = x_batch.to(device), mask.to(device), y_batch.to(device)
        optimizer.zero_grad()
        outputs = model(x_batch, mask=mask)
        loss = criterion(outputs, y_batch)
        loss.backward()
        optimizer.step()
        torch.cuda.empty_cache()

        losses.append(loss.item())
        preds = outputs.argmax(dim=1).cpu().numpy()
        all_preds.extend(preds)
        all_labels.extend(y_batch.cpu().numpy())

    acc = accuracy_score(all_labels, all_preds)
    precision, recall, f1, _ = precision_recall_fscore_support(all_labels, all_preds, average='weighted')
    return sum(losses)/len(losses), acc, precision, recall, f1

def eval_epoch(model, dataloader, criterion, device):
    model.eval()
    losses = []
    all_preds, all_labels = [], []

    with torch.no_grad():
        for x_batch, mask, y_batch in dataloader:
            x_batch, mask, y_batch = x_batch.to(device), mask.to(device), y_batch.to(device)
            outputs = model(x_batch, mask=mask)
            loss = criterion(outputs, y_batch)
            losses.append(loss.item())
            preds = outputs.argmax(dim=1).cpu().numpy()
            all_preds.extend(preds)
            all_labels.extend(y_batch.cpu().numpy())

    acc = accuracy_score(all_labels, all_preds)
    precision, recall, f1, _ = precision_recall_fscore_support(all_labels, all_preds, average='weighted')
    return sum(losses)/len(losses), acc, precision, recall, f1



def train_model(encoder, train_loader, val_loader, d_model=192, num_classes=2, epochs=8, lr=1e-3, device='cuda'):
    model = TabularTransformerWithClassifier(encoder, d_model, num_classes).to(device)
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(model.parameters(), lr=lr, weight_decay=1e-4)

    for epoch in range(1, epochs+1):
        train_loss, train_acc, train_prec, train_rec, train_f1 = train_epoch(model, train_loader, criterion, optimizer, device)
        val_loss, val_acc, val_prec, val_rec, val_f1 = eval_epoch(model, val_loader, criterion, device)

        print(f"Epoch {epoch}/{epochs}")
        print(f"Train loss: {train_loss:.4f} | Acc: {train_acc:.4f} | Prec: {train_prec:.4f} | Rec: {train_rec:.4f} | F1: {train_f1:.4f}")
        print(f"Val   loss: {val_loss:.4f} | Acc: {val_acc:.4f} | Prec: {val_prec:.4f} | Rec: {val_rec:.4f} | F1: {val_f1:.4f}")

    return model



batch_size = 16
d_model = 192


# Collect all data file pairs
file_pairs = collect_data_and_masks("./preprocessed_data/tabular/train", "./augmented_data/tabular")

# train_pairs, val_pairs = train_test_split(file_pairs, test_size=0.3, random_state=42, shuffle=True)

# train_dataset = SpiralDataset(train_pairs)
# val_dataset = SpiralDataset(val_pairs)

# train_loader = DataLoader(train_dataset, batch_size=16, shuffle=True, collate_fn=spiral_collate_fn)
# val_loader = DataLoader(val_dataset, batch_size=8, shuffle=False, collate_fn=spiral_collate_fn)

# encoder = TabularTransformer(input_dim=5, d_model=d_model)
# trained_model = train_model(encoder, train_loader, val_loader, d_model=d_model, num_classes=2, epochs=8, device=device)

# del train_loader
# del val_loader
# torch.cuda.empty_cache()
# print("training complete!")


all_features = []
all_labels = []

model = TabularTransformer(input_dim=5, d_model=d_model)
model.eval().to(device)  # Move to GPU if available

dataset = SpiralDataset(file_pairs)
dataloader = DataLoader(
    dataset,
    batch_size=batch_size,
    shuffle=True,
    collate_fn=spiral_collate_fn,
    num_workers=0
)

with torch.no_grad():
    for batch in tqdm(dataloader, desc="Extracting features"):
        spiral_seq, mask, labels = batch  # [B, T, 5], [B, T], [B]
        spiral_seq = spiral_seq.to(device)
        mask = mask.to(device)

        # features = trained_model.encoder(spiral_seq, mask=mask)  # [B, d_model]
        features = model(spiral_seq, mask=mask)  # [B, d_model]

        all_features.append(features.cpu())
        all_labels.append(labels)

all_features = torch.cat(all_features, dim=0)  # [N, d_model]
all_labels = torch.cat(all_labels, dim=0)      # [N]

df = pd.DataFrame(all_features.cpu().numpy())
labels_df = pd.DataFrame(all_labels.cpu().numpy())

df.to_csv('./encoders/encoded/tabular/untrained_features.csv', index=False)
labels_df.to_csv('./encoders/encoded/tabular/untrained_labelstest.csv', index=False)


print("Feature saving complete!")