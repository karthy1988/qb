from glob import glob
from collections import defaultdict
from string import punctuation
from functools import lru_cache

import kenlm
import nltk
from unidecode import unidecode
from qanta.pattern3 import pluralize
from nltk.tokenize import word_tokenize

from qanta.extractors.abstract import FeatureExtractor
from util.cached_wikipedia import CachedWikipedia
from clm.lm_wrapper import kTOKENIZER, LanguageModelBase
from qanta.util.environment import data_path
from util.build_whoosh import text_iterator

from nltk.corpus import wordnet as wn


@lru_cache(maxsize=None)
def get_states():
    states = set()
    for ii in wn.synset("American_state.n.1").instance_hyponyms():
        for jj in ii.lemmas():
            name = jj.name()
            if len(name) > 2 and "_" not in name:
                states.add(name)
            elif name.startswith("New_"):
                states.add(name.replace("New_", ""))
    return states


def find_references(sentence, padding=5):
    tags = nltk.pos_tag(word_tokenize(sentence))
    tags.append(("END", "V"))
    states = get_states()

    references_found = []
    this_ref_start = -1
    for ii, pair in enumerate(tags):
        word, tag = pair
        if word.lower() == 'this' or word.lower() == 'these':
            this_ref_start = ii
        elif all(x in punctuation for x in word):
            continue
        elif word in states:
            continue
        elif this_ref_start >= 0 and tag.startswith('NN') and \
                not tags[ii + 1][1].startswith('NN'):
            references_found.append((this_ref_start, ii))
            this_ref_start = -1
        elif tag.startswith('V'):
            this_ref_start = -1

    for start, stop in references_found:
        yield (" ".join(LanguageModelBase.normalize_title('', x[0])
                        for x in tags[max(0, start - padding):start]),
               " ".join(LanguageModelBase.normalize_title('', x[0])
                        for x in tags[start:stop + 1]),
               " ".join(LanguageModelBase.normalize_title('', x[0])
                        for x in tags[stop + 1:stop + padding + 1]))


def build_lm_data(path="data/wikipedia", output="temp/wiki_sent"):
    cw = CachedWikipedia(path, "")
    o = open(output, 'w')

    count = 0
    for ii in [x.split("/")[-1] for x in glob("%s/*" % path)]:
        count += 1
        if count % 1000 == 0:
            print("%i\t%s" % (count, unidecode(ii)))
        page = cw[ii]

        for ss in nltk.sent_tokenize(page.content):
            o.write("%s\n" % " ".join(kTOKENIZER(unidecode(ss.lower()))))


class Mentions(FeatureExtractor):
    def vw_from_score(self, results):
        pass

    def __init__(self, answers):
        super().__init__()
        self.name = "mentions"
        self.answers = answers
        self.initialized = False
        self.refex_count = defaultdict(int)
        self.refex_lookup = defaultdict(set)
        self.lm = None
        self.generate_refexs(self.answers)
        self.pre = []
        self.ment = []
        self.suf = []
        self.text = ""

    def set_metadata(self, answer, category, qnum, sent, token, guesses, fold):
        if not self.initialized:
            self.lm = kenlm.LanguageModel(data_path('data/kenlm.binary'))
            self.initialized = True

    def vw_from_title(self, title, text):
        # Find mentions if the text has changed
        if text != self.text:
            self.text = text
            self.pre = []
            self.ment = []
            self.suf = []
            # Find prefixes, suffixes, and mentions
            for pp, mm, ss in find_references(text):
                # Exclude too short mentions
                if len(mm.strip()) > 3:
                    self.pre.append(unidecode(pp.lower()))
                    self.suf.append(unidecode(ss.lower()))
                    self.ment.append(unidecode(mm.lower()))

        best_score = float("-inf")
        for ref in self.referring_exs(title):
            for pp, ss in zip(self.pre, self.suf):
                pre_tokens = kTOKENIZER(pp)
                ref_tokens = kTOKENIZER(ref)
                suf_tokens = kTOKENIZER(ss)

                query_len = len(pre_tokens) + len(ref_tokens) + len(suf_tokens)
                query = " ".join(pre_tokens + ref_tokens + suf_tokens)
                score = self.lm.score(query)
                if score > best_score:
                    best_score = score / float(query_len)
        if best_score > float("-inf"):
            res = "|%s score:%f" % (self.name, best_score)
        else:
            res = "|%s missing:1" % self.name

        norm_title = LanguageModelBase.normalize_title('', unidecode(title))
        assert ":" not in norm_title
        for mm in self.ment:
            assert ":" not in mm
            res += " "
            res += ("%s~%s" % (norm_title, mm)).replace(" ", "_")

        return res

    def generate_refexs(self, answer_list):
        """
        Given all of the possible answers, generate the referring expressions to
        store in dictionary.
        """

        # TODO: Make referring expression data-driven

        for aa in answer_list:
            ans = aa.split("_(")[0]
            for jj in ans.split():
                # each word and plural form of each word
                self.refex_lookup[aa].add(jj.lower())
                self.refex_lookup[aa].add(pluralize(jj).lower())
                self.refex_count[jj] += 1
                self.refex_count[pluralize(jj)] += 1

            # answer and plural form
            self.refex_count[ans.lower()] += 1
            self.refex_count[pluralize(ans).lower()] += 1
            self.refex_lookup[aa].add(ans.lower())
            self.refex_lookup[aa].add(pluralize(ans).lower())

            # THE answer
            self.refex_count["the %s" % ans.lower()] += 1
            self.refex_lookup[aa].add("the %s" % ans.lower())

    def referring_exs(self, answer, max_count=5):
        """

        Given a Wikipedia page, generate all of the referring expressions.
        Right now just rule-based, but should be improved.
        """
        for ii in self.refex_lookup[answer]:
            if self.refex_count[ii] < max_count:
                yield ii


def main():
    import argparse
    parser = argparse.ArgumentParser(description='')
    parser.add_argument('--build_lm_data', default=False, action='store_true',
                        help="Write current subset of wikipedia to build language model")
    parser.add_argument('--demo', default=False, action='store_true',
                        help="Demo mention scoring")
    parser.add_argument('--lm', default='data/kenlm.arpa', type=str,
                        help="Wikipedia language model")
    parser.add_argument("--min_answers", type=int, default=5,
                        help="Min answers")
    parser.add_argument("--db", type=str,
                        default="data/questions.db",
                        help="Location of questions")
    flags = parser.parse_args()

    if flags.build_lm_data:
        build_lm_data()
    DEMO_SENT = [
        "A 2011 play about this character was produced in collaboration between Rokia Traore, Peter Sellars, and Toni Morrison.",
        "The founder of this movement was inspired to develop its style by the stained glass windows he made for the De Lange House.",
        "Calvin Bridges sketched a specific type of these structures that contain diffuse regions called Balbiani rings and puffs.",
        "This group is represented by a dove in the Book of the Three Birds, written by a Welsh member of this group named Morgan Llwyd. A member of this religious group adopted the pseudonym 'Martin Marprelate' to pen a series of attacks against authorities.",
        "This leader spent three days in house arrest during an event masterminded by the 'Gang of Eight.'"]
    DEMO_GUESS = ["Desdemona", "De Stijl", "Mikhail Gorbachev", "Chromosome"]
    if flags.demo:
        answers = set(x for x, y in text_iterator(
            False, "", False, flags.db, False, "", limit=-1, min_pages=flags.min_answers))
        ment = Mentions(answers)

        # Show the mentions
        for ii in DEMO_GUESS:
            print(ii, list(ment.referring_exs(ii)))

        for ii in DEMO_SENT:
            print(ii)
            for jj in find_references(ii):
                print("\t%s\t|%s|\t%s" % jj)
            for jj in DEMO_GUESS:
                print(ment.vw_from_title(jj, ii))


if __name__ == "__main__":
    main()
