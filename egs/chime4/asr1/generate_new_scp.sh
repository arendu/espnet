#!/bin/bash
. cmd.sh

sdir=/export/corpora4/CHiME4/CHiME3/data/audio/16kHz/isolated_6ch_track/
dir=multi_track

allfiles=`find $sdir -name *.wav`
#allfiles=`find $sdir -name *.wav | head -n 800`

mkdir -p $dir

[ -f $dir/wav.scp ] && rm $dir/wav.scp

for i in $allfiles; do
  folder=$(echo $i | sed "s=.wav==g" | awk -F '/' '{print $(NF-1)}')
  key=$(echo $i | sed "s=.wav==g" | awk -F '/' '{print $NF}')
  key=$(echo $key | sed "s=\.=_=g")
  echo ${folder}_$key $i
done > $dir/wav.scp

mv $dir/wav.scp $dir/wav.scp.old
cat $dir/wav.scp.old | sort -u > $dir/wav.scp
cat $dir/wav.scp | awk '{print $1, $1}' | sort -u > $dir/spk2utt
cat $dir/wav.scp | awk '{print $1, $1}' | sort -u > $dir/utt2spk

steps/make_fbank_pitch.sh --nj 8 --cmd "${train_cmd}" $dir/ $dir/make_fbank $dir/fbank
