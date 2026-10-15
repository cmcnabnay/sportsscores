// team_logos.cpp
//
// Finds a logo and/or flag on Wikipedia for every team in the Supabase
// `matches` table and upserts them into the `teams` table, keyed by
// (name, sport) - the same name can need a different flag in a different
// sport (e.g. Ireland's rugby union team plays under the IRFU flag).
//
//   National teams  -> flag only. A team counts as national when it plays in
//                      a national-team competition (NATIONAL_LEAGUE_PREFIXES)
//                      and Wikipedia has a "Template:Country data <name>"
//                      for it; that template names the flag file the way
//                      Wikipedia's own sports articles show it (so e.g.
//                      Chinese Taipei gets its Olympic flag).
//   Clubs           -> logo (from the article's infobox) + the club's
//                      country flag (country from Wikidata P17, refined to
//                      England/Scotland/Wales/Ireland from the article's
//                      categories for UK clubs). matches.html shows the flag
//                      only in international club competitions.
//
// By default only teams not yet in `teams` are looked up, so it's cheap to
// re-run whenever new teams appear. Manual fixes go in overrides.json.
//
// Usage (from the repo root, reads ./.env.local):
//   team_logos/team_logos                 look up teams missing from `teams`
//   team_logos/team_logos --all           re-check every team
//   team_logos/team_logos --team NAME     re-check one team (repeatable)
//   team_logos/team_logos --dry-run       print results, write nothing
//   team_logos/team_logos --limit N       only the first N teams (testing)

#include <curl/curl.h>
#include <nlohmann/json.hpp>

#include <algorithm>
#include <chrono>
#include <fstream>
#include <iostream>
#include <map>
#include <optional>
#include <regex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

using json = nlohmann::json;
using Clock = std::chrono::steady_clock;

static const char* USER_AGENT =
    "sportsscores-team-logos/1.0 (https://github.com/cmcnabnay/sportsscores)";
static const char* WIKI_API = "https://en.wikipedia.org/w/api.php";
static const char* WIKIDATA_API = "https://www.wikidata.org/w/api.php";

// Gap between Wikimedia requests. Bursts get HTTP 429, and the scraper
// (fetch_fixtures.py) shares this IP, so stay well under the limit.
static const auto WIKI_REQUEST_GAP = std::chrono::milliseconds(1000);
static const int WIKI_BATCH = 50;          // max titles/ids per API request
static const int THUMB_WIDTH = 80;         // px; shown at ~20px, sharp on hi-dpi
static const int SUPABASE_PAGE_SIZE = 1000;

// Competitions between national teams. Season-independent key prefixes.
static const std::vector<std::string> NATIONAL_LEAGUE_PREFIXES = {
    "u20-jwc-", "rlwc-", "nations-championship-", "nations-cup-",
    "internationals-", "pacific-nations-cup-", "fiba-wcq-", "fivb-",
};

// International club competitions - clubs from several countries, so they
// say nothing about a club's country. Keep in sync with matches.html.
static const std::vector<std::string> INTL_CLUB_LEAGUE_PREFIXES = {
    "euroleague-", "eurocup-", "bcl-", "fiba-europe-cup-", "champions-cup-", "challenge-cup-",
};

// Per-sport search keywords and the infobox an article must have to count
// as that sport's club (case-insensitive regex on the infobox name).
struct SportInfo { std::string search; std::string infobox_re; std::string exclude_re; };
static const std::map<std::string, SportInfo> SPORTS = {
    {"basketball",        {"basketball",        "basketball", ""}},
    {"rugby",             {"rugby union",       "rugby",      "league"}},
    {"rugby-league",      {"rugby league",      "rugby league", ""}},
    // Some sports' club infoboxes are named after a league: hockey clubs
    // everywhere (DEL, SHL, ...) use "Infobox KHL team", CFL clubs "CFL team".
    {"hockey",            {"ice hockey",        "hockey|KHL|NHL", ""}},
    {"football",          {"football club",     "football",   ""}},
    {"american-football", {"American football", "football|NFL|ELF", ""}},
    {"canadian-football", {"Canadian football", "football|CFL", ""}},
    {"volleyball",        {"volleyball",        "volleyball", ""}},
};

// Bracket/placeholder names that aren't real teams.
static const std::regex PLACEHOLDER_RE(
    R"(^(TBD|TBC|TBA)$|^(winner|loser|runner-up)s?\b|^\d+(st|nd|rd|th)\b|\bplace$|^[WL]\d+$|^(pool|group)\s)",
    std::regex::icase);

// ---------------------------------------------------------------------------
// HTTP

struct HttpResponse {
    long status = 0;
    std::string body;
    long retry_after = 0;  // seconds, from a Retry-After header
};

class Http {
public:
    Http() {
        curl_ = curl_easy_init();
        if (!curl_) throw std::runtime_error("curl_easy_init failed");
    }
    ~Http() { curl_easy_cleanup(curl_); }

    std::string escape(const std::string& s) {
        char* e = curl_easy_escape(curl_, s.c_str(), static_cast<int>(s.size()));
        std::string out(e);
        curl_free(e);
        return out;
    }

    HttpResponse request(const std::string& method, const std::string& url,
                         const std::vector<std::string>& headers = {},
                         const std::string& body = "") {
        HttpResponse resp;
        curl_easy_reset(curl_);
        curl_easy_setopt(curl_, CURLOPT_URL, url.c_str());
        curl_easy_setopt(curl_, CURLOPT_USERAGENT, USER_AGENT);
        curl_easy_setopt(curl_, CURLOPT_ACCEPT_ENCODING, "");  // gzip etc.
        curl_easy_setopt(curl_, CURLOPT_TIMEOUT, 60L);
        curl_easy_setopt(curl_, CURLOPT_FOLLOWLOCATION, 1L);
        curl_easy_setopt(curl_, CURLOPT_WRITEFUNCTION, &Http::on_body);
        curl_easy_setopt(curl_, CURLOPT_WRITEDATA, &resp.body);
        curl_easy_setopt(curl_, CURLOPT_HEADERFUNCTION, &Http::on_header);
        curl_easy_setopt(curl_, CURLOPT_HEADERDATA, &resp.retry_after);
        curl_slist* list = nullptr;
        for (const auto& h : headers) list = curl_slist_append(list, h.c_str());
        curl_easy_setopt(curl_, CURLOPT_HTTPHEADER, list);
        if (method == "POST") {
            curl_easy_setopt(curl_, CURLOPT_POST, 1L);
            curl_easy_setopt(curl_, CURLOPT_POSTFIELDS, body.c_str());
            curl_easy_setopt(curl_, CURLOPT_POSTFIELDSIZE, static_cast<long>(body.size()));
        }
        CURLcode rc = curl_easy_perform(curl_);
        curl_slist_free_all(list);
        if (rc != CURLE_OK)
            throw std::runtime_error(method + " " + url.substr(0, 80) + ": " + curl_easy_strerror(rc));
        curl_easy_getinfo(curl_, CURLINFO_RESPONSE_CODE, &resp.status);
        return resp;
    }

private:
    CURL* curl_;

    static size_t on_body(char* p, size_t sz, size_t n, void* out) {
        static_cast<std::string*>(out)->append(p, sz * n);
        return sz * n;
    }
    static size_t on_header(char* p, size_t sz, size_t n, void* out) {
        std::string line(p, sz * n);
        std::string lower = line;
        std::transform(lower.begin(), lower.end(), lower.begin(), ::tolower);
        if (lower.rfind("retry-after:", 0) == 0) {
            try { *static_cast<long*>(out) = std::stol(line.substr(12)); } catch (...) {}
        }
        return sz * n;
    }
};

// ---------------------------------------------------------------------------
// Helpers

static std::string trim(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\r\n");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t\r\n");
    return s.substr(a, b - a + 1);
}

static bool starts_with(const std::string& s, const std::string& prefix) {
    return s.rfind(prefix, 0) == 0;
}

static std::string join(const std::vector<std::string>& v, const std::string& sep) {
    std::string out;
    for (size_t i = 0; i < v.size(); ++i) out += (i ? sep : "") + v[i];
    return out;
}

template <typename T>
static std::vector<std::vector<T>> chunks(const std::vector<T>& v, size_t n) {
    std::vector<std::vector<T>> out;
    for (size_t i = 0; i < v.size(); i += n)
        out.emplace_back(v.begin() + i, v.begin() + std::min(v.size(), i + n));
    return out;
}

static std::map<std::string, std::string> load_env_file(const std::string& path) {
    std::map<std::string, std::string> env;
    std::ifstream f(path);
    std::string line;
    while (std::getline(f, line)) {
        line = trim(line);
        if (line.empty() || line[0] == '#') continue;
        size_t eq = line.find('=');
        if (eq == std::string::npos) continue;
        std::string v = trim(line.substr(eq + 1));
        if (v.size() >= 2 && (v.front() == '"' || v.front() == '\'') && v.back() == v.front())
            v = v.substr(1, v.size() - 2);
        env[trim(line.substr(0, eq))] = v;
    }
    return env;
}

static std::string strip_comments(const std::string& s) {
    static const std::regex comment("<!--[\\s\\S]*?(-->|$)");
    return std::regex_replace(s, comment, "");
}

// "Atl%C3%A9tico" -> "Atlético" (some infoboxes paste URL-encoded names)
static std::string percent_decode(const std::string& s) {
    std::string out;
    for (size_t i = 0; i < s.size(); ++i) {
        if (s[i] == '%' && i + 2 < s.size() && std::isxdigit(static_cast<unsigned char>(s[i + 1])) &&
            std::isxdigit(static_cast<unsigned char>(s[i + 2]))) {
            out += static_cast<char>(std::stoi(s.substr(i + 1, 2), nullptr, 16));
            i += 2;
        } else {
            out += s[i];
        }
    }
    return out;
}

// "[[File:Foo.svg|150px]]" / "File:Foo.svg" / "Foo.svg" -> "Foo.svg"
static std::string clean_file_name(std::string v) {
    v = strip_comments(v);
    size_t open = v.find("[[");
    if (open != std::string::npos) v = v.substr(open + 2);
    v = v.substr(0, v.find_first_of("|]}"));
    v = trim(v);
    static const std::regex prefix("^(file|image)\\s*:\\s*", std::regex::icase);
    v = std::regex_replace(v, prefix, "");
    if (v.find('%') != std::string::npos) v = percent_decode(v);
    std::replace(v.begin(), v.end(), '_', ' ');
    static const std::regex ext("\\.(svg|png|jpe?g|gif|webp)$", std::regex::icase);
    return std::regex_search(v, ext) ? v : "";
}

// ---------------------------------------------------------------------------
// API clients

class Wiki {
public:
    explicit Wiki(Http& http) : http_(http) {}

    json get(const std::string& base, std::map<std::string, std::string> params) {
        params["format"] = "json";
        params["formatversion"] = "2";
        params["maxlag"] = "5";
        std::string url = std::string(base) + "?";
        for (const auto& [k, v] : params) url += http_.escape(k) + "=" + http_.escape(v) + "&";
        for (int attempt = 0;; ++attempt) {
            throttle();
            HttpResponse r = http_.request("GET", url);
            bool lagged = false;
            json j;
            if (r.status == 200) {
                j = json::parse(r.body, nullptr, false);
                if (j.is_discarded()) throw std::runtime_error("bad JSON from " + base);
                lagged = j.contains("error") && j["error"].value("code", "") == "maxlag";
                if (!lagged) {
                    if (j.contains("error"))
                        throw std::runtime_error("Wikimedia API error: " + j["error"].dump());
                    return j;
                }
            } else if (r.status != 429 && r.status < 500) {
                throw std::runtime_error("HTTP " + std::to_string(r.status) + " from " + base);
            }
            if (attempt >= 6)
                throw std::runtime_error("giving up after repeated HTTP " + std::to_string(r.status) +
                                         (lagged ? " (maxlag)" : "") + " from " + base);
            long wait = r.retry_after > 0 ? r.retry_after : (5L << attempt);  // 5,10,20..s
            std::cerr << "  .. Wikimedia " << (lagged ? "maxlag" : "HTTP " + std::to_string(r.status))
                      << ", waiting " << wait << "s\n";
            std::this_thread::sleep_for(std::chrono::seconds(wait));
        }
    }

private:
    Http& http_;
    std::optional<Clock::time_point> last_;

    void throttle() {
        if (last_) {
            auto next = *last_ + WIKI_REQUEST_GAP;
            if (Clock::now() < next) std::this_thread::sleep_until(next);
        }
        last_ = Clock::now();
    }
};

class Supabase {
public:
    Supabase(Http& http, std::string url, std::string key)
        : http_(http), url_(std::move(url)), key_(std::move(key)) {}

    json get(const std::string& path) {
        HttpResponse r = http_.request("GET", url_ + "/rest/v1/" + path, auth());
        check(r, "GET " + path.substr(0, path.find('?')));
        return json::parse(r.body);
    }

    json get_all(const std::string& path) {  // paginated past PostgREST's max rows
        json all = json::array();
        for (int offset = 0;; offset += SUPABASE_PAGE_SIZE) {
            json page = get(path + "&limit=" + std::to_string(SUPABASE_PAGE_SIZE) +
                            "&offset=" + std::to_string(offset));
            for (auto& row : page) all.push_back(std::move(row));
            if (page.size() < static_cast<size_t>(SUPABASE_PAGE_SIZE)) return all;
        }
    }

    void upsert(const std::string& table, const std::string& on_conflict, const json& rows) {
        auto headers = auth();
        headers.push_back("Content-Type: application/json");
        headers.push_back("Prefer: resolution=merge-duplicates,return=minimal");
        HttpResponse r = http_.request("POST", url_ + "/rest/v1/" + table + "?on_conflict=" + on_conflict,
                                       headers, rows.dump());
        check(r, "POST " + table);
    }

private:
    Http& http_;
    std::string url_, key_;

    std::vector<std::string> auth() const {
        return {"apikey: " + key_, "Authorization: Bearer " + key_};
    }
    static void check(const HttpResponse& r, const std::string& what) {
        if (r.status < 200 || r.status >= 300)
            throw std::runtime_error("Supabase " + what + " -> HTTP " + std::to_string(r.status) +
                                     ": " + r.body.substr(0, 400));
    }
};

// ---------------------------------------------------------------------------
// Lookups

struct Team {
    std::string name, sport;
    bool in_national_comp = false;
    bool national = false;
    std::string wiki_title, logo_file, country, flag_file;
    std::string logo_url, flag_url;      // direct image URLs from overrides.json
    std::string qid;                     // Wikidata item of the club article
    std::vector<std::string> categories; // of the club article
    std::set<std::string> domestic_leagues; // non-national competitions it plays in
    std::string note;                    // why nothing was found / what was guessed
};

static std::string key_of(const std::string& name, const std::string& sport) {
    return name + "|" + sport;
}

// Follow a query's "normalized" + "redirects" lists from a requested title
// to the title its page is listed under.
static std::string resolve_title(const json& q, std::string title) {
    for (const char* step : {"normalized", "redirects"}) {
        if (!q.contains(step)) continue;
        for (int hop = 0; hop < 5; ++hop) {
            bool moved = false;
            for (const auto& r : q[step])
                if (r["from"].get<std::string>() == title) { title = r["to"].get<std::string>(); moved = true; break; }
            if (!moved) break;
        }
    }
    return title;
}

// "Template:Country data <X>" -> its canonical name and flag file, for many
// X at once. Missing templates are left out of the result.
struct CountryFlag { std::string country, flag_file; };

static std::map<std::string, CountryFlag> country_flags(Wiki& wiki, const std::set<std::string>& names) {
    static const std::string P = "Template:Country data ";
    static const std::regex flag_re(R"(\|\s*flag alias\s*=\s*([^\n|}]+))");
    std::map<std::string, CountryFlag> out;
    std::vector<std::string> list(names.begin(), names.end());
    for (const auto& batch : chunks(list, WIKI_BATCH)) {
        std::vector<std::string> titles;
        for (const auto& n : batch) titles.push_back(P + n);
        json q = wiki.get(WIKI_API, {{"action", "query"}, {"prop", "revisions"}, {"rvprop", "content"},
                                     {"rvslots", "main"}, {"redirects", "1"}, {"titles", join(titles, "|")}})["query"];
        std::map<std::string, CountryFlag> by_title;
        for (const auto& p : q["pages"]) {
            if (p.value("missing", false) || !p.contains("revisions")) continue;
            std::string text = strip_comments(p["revisions"][0]["slots"]["main"].value("content", ""));
            std::smatch m;
            if (!std::regex_search(text, m, flag_re)) continue;
            std::string title = p["title"].get<std::string>();
            by_title[title] = {title.substr(P.size()), trim(m[1].str())};
        }
        for (const auto& n : batch) {
            std::string t = resolve_title(q, P + n);
            if (by_title.count(t)) out[n] = by_title[t];
        }
    }
    return out;
}

static bool infobox_matches(const std::string& text, const std::string& sport) {
    static const std::regex ib(R"(\{\{\s*Infobox[ _]+([^\n|}<]+))", std::regex::icase);
    std::smatch m;
    if (!std::regex_search(text, m, ib)) return false;
    std::string name = m[1].str();
    // Search results include players, seasons, venues, ... whose infoboxes
    // also mention the sport ("Infobox ice hockey player"). Only clubs count.
    static const std::regex club_re("club|team|franchise", std::regex::icase);
    static const std::regex not_club_re(
        "player|person|biography|coach|official|referee|season|national|stadium|arena|venue|competition|tournament",
        std::regex::icase);
    if (!std::regex_search(name, club_re) || std::regex_search(name, not_club_re)) return false;
    auto it = SPORTS.find(sport);
    if (it == SPORTS.end()) return true;
    if (!std::regex_search(name, std::regex(it->second.infobox_re, std::regex::icase))) return false;
    if (!it->second.exclude_re.empty() &&
        std::regex_search(name, std::regex(it->second.exclude_re, std::regex::icase)))
        return false;
    return true;
}

static std::string infobox_logo(const std::string& text) {
    for (const char* param : {"logo", "crest", "current_logo", "team_logo", "image"}) {
        std::regex re(std::string(R"((?:^|\n)\s*\|\s*)") + param + R"(\s*=\s*([^\n]*))", std::regex::icase);
        std::smatch m;
        if (std::regex_search(text, m, re)) {
            std::string f = clean_file_name(m[1].str());
            if (!f.empty()) return f;
        }
    }
    return "";
}

// "Śląsk Wrocław" -> "Slask Wroclaw": Latin letters with diacritics folded
// to ASCII, so names match whether or not the source kept the accents.
static std::string fold_diacritics(const std::string& s) {
    static const std::map<char, std::string> groups = {
        {'a', "àáâãäåāăąÀÁÂÃÄÅĀĂĄ"}, {'c', "çćĉċčÇĆĈĊČ"}, {'d', "ďđĎĐ"},
        {'e', "èéêëēĕėęěÈÉÊËĒĔĖĘĚ"}, {'g', "ĝğġģĜĞĠĢ"}, {'i', "ìíîïĩīįıÌÍÎÏĨĪĮİ"},
        {'k', "ķĶ"}, {'l', "ĺļľŀłĹĻĽĿŁ"}, {'n', "ñńņňÑŃŅŇ"}, {'o', "òóôõöøōőÒÓÔÕÖØŌŐ"},
        {'r', "ŕŗřŔŖŘ"}, {'s', "śŝşšșŚŜŞŠȘ"}, {'t', "ţťțŢŤȚ"}, {'u', "ùúûüũūůűųÙÚÛÜŨŪŮŰŲ"},
        {'y', "ýÿÝŸ"}, {'z', "źżžŹŻŽ"},
    };
    static const std::map<std::string, char> table = [] {
        std::map<std::string, char> t;
        for (const auto& [ascii, chars] : groups)
            for (size_t i = 0; i < chars.size(); i += 2) t[chars.substr(i, 2)] = ascii;  // all 2-byte UTF-8
        return t;
    }();
    std::string out;
    for (size_t i = 0; i < s.size(); ++i) {
        if (i + 1 < s.size()) {
            auto it = table.find(s.substr(i, 2));
            if (it != table.end()) { out += it->second; ++i; continue; }
        }
        out += s[i];
    }
    return out;
}

// Lowercased 3+ letter ASCII words of s (diacritics folded), minus words
// that say nothing about which club it is.
static std::set<std::string> name_words(const std::string& raw) {
    const std::string s = fold_diacritics(raw);
    static const std::set<std::string> stop = {
        "the", "club", "team", "basketball", "basket", "rugby", "hockey", "football", "union",
        "league", "sport", "sports", "and", "des", "del", "los", "las", "bc", "fc", "rfc",
    };
    std::set<std::string> out;
    std::string w;
    for (char c : s + " ") {
        if (std::isalpha(static_cast<unsigned char>(c)) && static_cast<unsigned char>(c) < 128) {
            w += static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
        } else {
            if (w.size() >= 3 && !stop.count(w)) out.insert(w);
            w.clear();
        }
    }
    return out;
}

// Does the article name this team? Compares the team name's words with the
// article title, the infobox's name fields and the bold names in the lead
// ('''...'''), allowing small spelling changes (same first 5 letters:
// "Harlequins"/"Harlequin", "Reggio"/"Reggiana"). Stops a search picking an
// opponent's article that merely mentions the team.
static bool article_names_team(const std::string& team, const std::string& title, const std::string& text) {
    std::string names = title;
    static const std::regex field(
        R"((?:^|\n)\s*\|\s*(?:name|full_?name|team|short_?name|nickname|sponsor\w*|common_name)\s*=\s*([^\n]*))",
        std::regex::icase);
    for (auto it = std::sregex_iterator(text.begin(), text.end(), field); it != std::sregex_iterator(); ++it)
        names += " " + (*it)[1].str();
    static const std::regex bold("'''(.+?)'''");
    for (auto it = std::sregex_iterator(text.begin(), text.end(), bold); it != std::sregex_iterator(); ++it)
        names += " " + (*it)[1].str();
    auto prefix = [](const std::string& w) { return w.substr(0, std::min<size_t>(5, w.size())); };
    std::set<std::string> have;
    for (const auto& w : name_words(names)) have.insert(prefix(w));
    for (const auto& w : name_words(team))
        if (have.count(prefix(w))) return true;
    return false;
}

// Find the club's article and read its logo, Wikidata item and categories.
// Tries, in order: the overridden title; the team's exact name (Wikipedia
// redirects usually point that at the club - "Dinamo Minsk" -> "HC Dinamo
// Minsk"); a search. Each candidate must have a club infobox for the sport,
// so disambiguation pages, cities and other sports' clubs are skipped.
static bool lookup_club_query(Wiki& wiki, Team& t, std::map<std::string, std::string> p, bool forced,
                              bool check_name = false);

static void lookup_club(Wiki& wiki, Team& t) {
    if (!t.wiki_title.empty()) {
        if (!lookup_club_query(wiki, t, {{"titles", t.wiki_title}}, true)) t.note = "override article not found";
        return;
    }
    if (lookup_club_query(wiki, t, {{"titles", t.name}}, false)) return;
    auto it = SPORTS.find(t.sport);
    if (lookup_club_query(wiki, t, {{"generator", "search"},
                                    {"gsrsearch", t.name + " " + (it != SPORTS.end() ? it->second.search : t.sport)},
                                    {"gsrlimit", "10"}, {"gsrnamespace", "0"}}, false, true))
        return;
    t.note = "no " + t.sport + " club article found";
}

static bool lookup_club_query(Wiki& wiki, Team& t, std::map<std::string, std::string> p, bool forced,
                              bool check_name) {
    p.insert({
        {"action", "query"}, {"redirects", "1"},
        {"prop", "revisions|pageprops|pageimages|categories"},
        {"rvprop", "content"}, {"rvslots", "main"}, {"rvsection", "0"},
        {"ppprop", "wikibase_item"}, {"piprop", "name"}, {"pilicense", "any"},
        {"cllimit", "max"}, {"clshow", "!hidden"},
    });
    json q = wiki.get(WIKI_API, p);
    if (!q.contains("query") || !q["query"].contains("pages")) return false;
    std::vector<json> pages(q["query"]["pages"].begin(), q["query"]["pages"].end());
    std::sort(pages.begin(), pages.end(),
              [](const json& a, const json& b) { return a.value("index", 0) < b.value("index", 0); });
    for (const auto& pg : pages) {
        if (pg.value("missing", false) || !pg.contains("revisions")) continue;
        std::string title = pg["title"].get<std::string>();
        std::string text = pg["revisions"][0]["slots"]["main"].value("content", "");
        if (!forced && (std::isdigit(static_cast<unsigned char>(title[0])) ||
                        title.find(" season") != std::string::npos))
            continue;
        if (!forced && !infobox_matches(text, t.sport)) continue;
        if (check_name && !article_names_team(t.name, title, text)) continue;
        t.wiki_title = title;
        t.logo_file = infobox_logo(text);
        if (t.logo_file.empty() && pg.contains("pageimage")) t.logo_file = clean_file_name(pg["pageimage"].get<std::string>());
        if (pg.contains("pageprops")) t.qid = pg["pageprops"].value("wikibase_item", "");
        if (pg.contains("categories"))
            for (const auto& c : pg["categories"]) t.categories.push_back(c["title"].get<std::string>());
        if (t.logo_file.empty()) t.note = "article has no logo";
        return true;
    }
    return false;
}

// Wikidata: club item -> country item (P17) -> English Wikipedia title.
static void lookup_countries(Wiki& wiki, std::vector<Team*>& clubs) {
    std::vector<std::string> qids;
    for (auto* t : clubs) if (!t->qid.empty() && t->country.empty()) qids.push_back(t->qid);
    std::map<std::string, std::string> club_country_qid;
    for (const auto& batch : chunks(qids, WIKI_BATCH)) {
        json e = wiki.get(WIKIDATA_API, {{"action", "wbgetentities"}, {"props", "claims"},
                                         {"ids", join(batch, "|")}})["entities"];
        for (const auto& el : e.items()) {
            const std::string& id = el.key();
            const json& ent = el.value();
            if (!ent.contains("claims")) continue;
            std::string best;
            for (const auto& c : ent["claims"].value("P17", json::array())) {
                if (c.value("rank", "") == "deprecated") continue;
                const auto& snak = c["mainsnak"];
                if (!snak.contains("datavalue")) continue;
                std::string v = snak["datavalue"]["value"]["id"].get<std::string>();
                if (best.empty() || c.value("rank", "") == "preferred") best = v;
            }
            if (!best.empty()) club_country_qid[id] = best;
            // Logo fallback for articles whose infobox has none (Wikidata
            // only holds freely licensed logos, so it's often empty).
            if (ent["claims"].contains("P154") && !ent["claims"]["P154"].empty())
                for (auto* t : clubs)
                    if (t->qid == id && t->logo_file.empty()) {
                        const auto& snak = ent["claims"]["P154"][0]["mainsnak"];
                        if (snak.contains("datavalue"))
                            t->logo_file = clean_file_name(snak["datavalue"]["value"].get<std::string>());
                        if (!t->logo_file.empty()) t->note = "";
                    }
        }
    }
    std::set<std::string> cset;
    for (const auto& [club, c] : club_country_qid) cset.insert(c);
    std::vector<std::string> cq(cset.begin(), cset.end());
    std::map<std::string, std::string> country_title;
    for (const auto& batch : chunks(cq, WIKI_BATCH)) {
        json e = wiki.get(WIKIDATA_API, {{"action", "wbgetentities"}, {"props", "sitelinks"},
                                         {"sitefilter", "enwiki"}, {"ids", join(batch, "|")}})["entities"];
        for (const auto& el : e.items())
            if (el.value().contains("sitelinks") && el.value()["sitelinks"].contains("enwiki"))
                country_title[el.key()] = el.value()["sitelinks"]["enwiki"]["title"].get<std::string>();
    }
    for (auto* t : clubs) {
        if (t->country.empty() && club_country_qid.count(t->qid))
            t->country = country_title[club_country_qid[t->qid]];
        if (t->country == "Republic of Ireland") t->country = "Ireland";
        // UK clubs: Wikidata says "United Kingdom"; sports flags are per
        // home nation. The article's categories say which ("Rugby union
        // teams in England", "Scottish professional rugby union teams", ...).
        // Northern Ireland is checked first, and "Irish " ignores
        // "Northern Irish ", so Ulster-style clubs aren't read as Ireland.
        if (t->country == "United Kingdom" || t->country.empty()) {
            static const std::vector<std::pair<std::string, std::string>> nations = {
                {"Northern Ireland", "Northern Ireland"}, {"Ireland", "Ireland"},
                {"England", "England"}, {"Scotland", "Scotland"}, {"Wales", "Wales"},
            };
            static const std::map<std::string, std::string> adjective = {
                {"Northern Ireland", "Northern Irish"}, {"Ireland", "Irish"},
                {"England", "English"}, {"Scotland", "Scottish"}, {"Wales", "Welsh"},
            };
            for (const auto& [nation_name, nation] : nations) {
                const std::string in = " in " + nation_name;
                const std::string adj = adjective.at(nation_name) + " ";
                bool hit = std::any_of(t->categories.begin(), t->categories.end(), [&](const std::string& c) {
                    bool in_hit = c.size() >= in.size() && c.compare(c.size() - in.size(), in.size(), in) == 0;
                    bool adj_hit = c.find(":" + adj) != std::string::npos ||
                                   (c.find(" " + adj) != std::string::npos &&
                                    !(nation_name == "Ireland" && c.find("Northern Irish ") != std::string::npos));
                    return in_hit || adj_hit;
                });
                if (hit) { t->country = nation; break; }
            }
        }
    }
}

// File names -> 80px thumbnail URLs (PNG renderings of SVGs).
static std::map<std::string, std::string> image_urls(Wiki& wiki, const std::set<std::string>& files) {
    std::map<std::string, std::string> out;
    std::vector<std::string> list(files.begin(), files.end());
    for (const auto& batch : chunks(list, WIKI_BATCH)) {
        std::vector<std::string> titles;
        for (const auto& f : batch) titles.push_back("File:" + f);
        json q = wiki.get(WIKI_API, {{"action", "query"}, {"prop", "imageinfo"}, {"iiprop", "url"},
                                     {"iiurlwidth", std::to_string(THUMB_WIDTH)}, {"redirects", "1"},
                                     {"titles", join(titles, "|")}})["query"];
        std::map<std::string, std::string> by_title;
        for (const auto& p : q["pages"]) {
            if (!p.contains("imageinfo")) continue;
            const auto& ii = p["imageinfo"][0];
            std::string u = ii.value("thumburl", ii.value("url", ""));
            u = u.substr(0, u.find('?'));  // drop utm_* tracking params
            by_title[p["title"].get<std::string>()] = u;
        }
        for (const auto& f : batch) {
            std::string t = resolve_title(q, "File:" + f);
            if (by_title.count(t)) out[f] = by_title[t];
        }
    }
    return out;
}

// ---------------------------------------------------------------------------

int main(int argc, char** argv) {
    bool all = false, dry_run = false;
    long limit = -1;
    std::set<std::string> only;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--all") all = true;
        else if (a == "--dry-run") dry_run = true;
        else if (a == "--team" && i + 1 < argc) only.insert(argv[++i]);
        else if (a == "--limit" && i + 1 < argc) limit = std::stol(argv[++i]);
        else {
            std::cerr << "usage: team_logos [--all] [--team NAME]... [--dry-run] [--limit N]\n";
            return 2;
        }
    }

    try {
        curl_global_init(CURL_GLOBAL_DEFAULT);
        // Only this repo's .env.local - never the shell's SUPABASE_* vars,
        // which belong to a different Supabase project.
        auto env = load_env_file(".env.local");
        std::string sb_url = std::regex_replace(env["SUPABASE_URL"], std::regex("/+(rest/v1/?)?$"), "");
        if (sb_url.empty() || env["SUPABASE_SECRET_KEY"].empty())
            throw std::runtime_error("SUPABASE_URL / SUPABASE_SECRET_KEY missing from ./.env.local "
                                     "(run from the repo root)");

        json overrides;
        {
            std::ifstream f("team_logos/overrides.json");
            if (!f) throw std::runtime_error("team_logos/overrides.json not found (run from the repo root)");
            overrides = json::parse(f);
        }
        json sport_country_flags = overrides.value("sport_country_flags", json::object());
        json team_overrides = overrides.value("teams", json::object());

        Http http;
        Supabase sb(http, sb_url, env["SUPABASE_SECRET_KEY"]);
        Wiki wiki(http);

        // --- Which teams exist ------------------------------------------------
        json meta = sb.get("fixtures_meta?id=eq.1&select=leagues:data->leagues,standings:data->standings,"
                           "attendance:data->attendance_tables");
        if (meta.empty()) throw std::runtime_error("fixtures_meta is empty");
        std::map<std::string, std::string> league_sport;
        for (const auto& el : meta[0]["leagues"].items()) league_sport[el.key()] = el.value().value("sport", "");

        std::map<std::string, Team> teams;
        for (const auto& m : sb.get_all("matches?select=league,home,away&order=match_id")) {
            std::string league = m["league"].get<std::string>();
            std::string sport = league_sport.count(league) ? league_sport[league] : "";
            bool national_comp = std::any_of(NATIONAL_LEAGUE_PREFIXES.begin(), NATIONAL_LEAGUE_PREFIXES.end(),
                                             [&](const std::string& p) { return starts_with(league, p); });
            for (const char* side : {"home", "away"}) {
                if (!m[side].is_string()) continue;
                std::string name = trim(m[side].get<std::string>());
                if (name.empty() || std::regex_search(name, PLACEHOLDER_RE)) continue;
                Team& t = teams[key_of(name, sport)];
                t.name = name;
                t.sport = sport;
                t.in_national_comp = t.in_national_comp || national_comp;
                bool intl_club = std::any_of(INTL_CLUB_LEAGUE_PREFIXES.begin(), INTL_CLUB_LEAGUE_PREFIXES.end(),
                                             [&](const std::string& p) { return starts_with(league, p); });
                if (!national_comp && !intl_club) t.domestic_leagues.insert(league);
            }
        }

        // Teams that only appear in standings/attendance tables (e.g. a
        // league where only one club's matches are scraped). Names there can
        // carry markers the matches don't ("Sydney Roosters (P)", "x - Leeds"),
        // cleaned like matches.html's normalizeTeamName(); a name that's just
        // a marked-up version of a match team is skipped.
        {
            static const std::regex lead_marker("^[a-zA-Z]\\s*(-|\xE2\x80\x93)\\s*");
            static const std::regex footnote("\\s*\\[[a-zA-Z0-9]{1,3}\\]\\s*$");
            static const std::regex status("\\s*\\((H|A|Q|X|T|h|a|q|x|t)\\)\\s*$");
            static const std::regex any_marker("\\s*\\([A-Z]{1,2}\\)$");
            auto add_table_team = [&](const std::string& league, const std::string& raw) {
                std::string sport = league_sport.count(league) ? league_sport[league] : "";
                std::string n1 = trim(std::regex_replace(std::regex_replace(std::regex_replace(
                    raw, lead_marker, ""), footnote, ""), status, ""));
                std::string n2 = trim(std::regex_replace(n1, any_marker, ""));
                if (sport.empty() || n1.empty() || std::regex_search(n1, PLACEHOLDER_RE)) return;
                if (teams.count(key_of(n1, sport)) || teams.count(key_of(n2, sport))) return;
                Team& t = teams[key_of(n1, sport)];
                t.name = n1;
                t.sport = sport;
                bool national_comp = std::any_of(NATIONAL_LEAGUE_PREFIXES.begin(), NATIONAL_LEAGUE_PREFIXES.end(),
                                                 [&](const std::string& p) { return starts_with(league, p); });
                bool intl_club = std::any_of(INTL_CLUB_LEAGUE_PREFIXES.begin(), INTL_CLUB_LEAGUE_PREFIXES.end(),
                                             [&](const std::string& p) { return starts_with(league, p); });
                t.in_national_comp = t.in_national_comp || national_comp;
                if (!national_comp && !intl_club) t.domestic_leagues.insert(league);
            };
            const json& st = meta[0].value("standings", json::object());
            for (const auto& lg : st.items()) {
                if (!lg.value().is_object()) continue;
                for (const auto& grp : lg.value().items()) {
                    const json& g = grp.value();
                    const json rows = g.is_array() ? g : g.value("rows", json::array());
                    for (const auto& r : rows)
                        if (r.contains("team") && r["team"].is_string()) add_table_team(lg.key(), r["team"].get<std::string>());
                }
            }
            const json& at = meta[0].value("attendance", json::object());
            for (const auto& lg : at.items())
                if (lg.value().is_array())
                    for (const auto& r : lg.value())
                        if (r.contains("team") && r["team"].is_string()) add_table_team(lg.key(), r["team"].get<std::string>());
        }

        std::set<std::string> existing;
        if (!all && only.empty())
            for (const auto& r : sb.get_all("teams?select=name,sport&order=name"))
                existing.insert(key_of(r["name"].get<std::string>(), r["sport"].get<std::string>()));

        std::vector<Team*> todo;
        for (auto& [k, t] : teams) {
            if (!only.empty() ? !only.count(t.name) : existing.count(k)) continue;
            todo.push_back(&t);
            if (limit >= 0 && static_cast<long>(todo.size()) >= limit) break;
        }
        std::cout << teams.size() << " teams in matches, " << existing.size() << " already in `teams`, "
                  << todo.size() << " to look up\n";
        if (todo.empty()) return 0;

        // --- Overrides ----------------------------------------------------------
        for (auto* t : todo) {
            std::string k = key_of(t->name, t->sport);
            if (!team_overrides.contains(k)) continue;
            const json& o = team_overrides[k];
            t->wiki_title = o.value("wiki_title", "");
            t->logo_file = o.value("logo_file", "");
            t->flag_file = o.value("flag_file", "");
            t->country = o.value("country", "");
            t->logo_url = o.value("logo_url", "");
            t->flag_url = o.value("flag_url", "");
            if (o.contains("national")) {
                t->national = o["national"].get<bool>();
                t->in_national_comp = t->national;
            }
        }

        // --- National teams: a Country data template for the name -----------
        std::set<std::string> nat_names;
        for (auto* t : todo) if (t->in_national_comp) nat_names.insert(t->country.empty() ? t->name : t->country);
        auto nat_flags = country_flags(wiki, nat_names);
        for (auto* t : todo) {
            if (!t->in_national_comp) continue;
            auto it = nat_flags.find(t->country.empty() ? t->name : t->country);
            if (it == nat_flags.end()) continue;  // e.g. Barbarians, Sharks: treat as a club
            t->national = true;
            t->country = it->second.country;
            if (t->flag_file.empty()) t->flag_file = it->second.flag_file;
        }

        // --- Clubs: article, logo, country ----------------------------------
        std::vector<Team*> clubs;
        for (auto* t : todo) if (!t->national) clubs.push_back(t);
        size_t n = 0;
        for (auto* t : clubs) {
            std::cout << "  [" << ++n << "/" << clubs.size() << "] " << t->name << " (" << t->sport << ")\n";
            if (t->logo_file.empty() || t->qid.empty()) {
                std::string keep_logo = t->logo_file;
                lookup_club(wiki, *t);
                if (!keep_logo.empty()) t->logo_file = keep_logo;
            }
        }
        lookup_countries(wiki, clubs);
        std::set<std::string> club_countries;
        for (auto* t : clubs) if (!t->country.empty() && t->flag_file.empty()) club_countries.insert(t->country);
        auto club_flags = country_flags(wiki, club_countries);
        for (auto* t : clubs) {
            if (!t->flag_file.empty() || !club_flags.count(t->country)) continue;
            const CountryFlag cf = club_flags[t->country];
            t->country = cf.country;
            t->flag_file = cf.flag_file;
        }

        // Still no country (no Wikidata P17): if every other club in one of
        // its domestic leagues is from the same country, use that.
        {
            std::map<std::string, std::set<std::string>> league_countries;
            for (const auto& [k, t] : teams)
                if (!t.national && !t.country.empty())
                    for (const auto& lg : t.domestic_leagues) league_countries[lg].insert(t.country);
            for (auto* t : clubs) {
                if (!t->country.empty() || !t->flag_file.empty()) continue;
                for (const auto& lg : t->domestic_leagues) {
                    const auto& cs = league_countries[lg];
                    if (cs.size() != 1) continue;
                    t->country = *cs.begin();
                    for (const auto& [k, o] : teams)
                        if (o.country == t->country && !o.flag_file.empty()) { t->flag_file = o.flag_file; break; }
                    t->note += " (country guessed from " + lg + ")";
                    break;
                }
            }
        }

        // Sport-specific flags (IRFU for Irish rugby), unless set per team.
        for (auto* t : todo) {
            std::string k = t->sport + "|" + t->country;
            bool per_team = team_overrides.contains(key_of(t->name, t->sport)) &&
                            team_overrides[key_of(t->name, t->sport)].contains("flag_file");
            if (!per_team && sport_country_flags.contains(k)) t->flag_file = sport_country_flags[k].get<std::string>();
        }

        // --- Image URLs ---------------------------------------------------------
        std::set<std::string> files;
        for (auto* t : todo) {
            if (!t->logo_file.empty()) files.insert(t->logo_file);
            if (!t->flag_file.empty()) files.insert(t->flag_file);
        }
        auto urls = image_urls(wiki, files);

        // --- Report + write -----------------------------------------------------
        json rows = json::array();
        std::vector<std::string> problems;
        auto or_null = [](const std::string& s) { return s.empty() ? json(nullptr) : json(s); };
        for (auto* t : todo) {
            std::string logo_url = !t->logo_url.empty() ? t->logo_url
                                   : urls.count(t->logo_file) ? urls[t->logo_file] : "";
            std::string flag_url = !t->flag_url.empty() ? t->flag_url
                                   : urls.count(t->flag_file) ? urls[t->flag_file] : "";
            if (!t->logo_url.empty()) t->note = "";
            if (t->logo_file.empty() && !t->logo_url.empty()) t->logo_file = "(url override)";
            std::cout << (t->national ? "[nation] " : "[club]   ") << t->name << " | " << t->sport << " -> "
                      << (t->wiki_title.empty() ? "-" : t->wiki_title) << " | logo: "
                      << (t->logo_file.empty() ? "-" : t->logo_file) << " | " << (t->country.empty() ? "-" : t->country)
                      << " flag: " << (t->flag_file.empty() ? "-" : t->flag_file) << "\n";
            if (!t->logo_file.empty() && logo_url.empty()) t->note += " logo file not found";
            if (!t->flag_file.empty() && flag_url.empty()) t->note += " flag file not found";
            bool missing = t->national ? flag_url.empty() : (logo_url.empty() || flag_url.empty());
            if (missing) problems.push_back(t->name + " | " + t->sport + ": " +
                                            (t->note.empty() ? "no country/flag found" : trim(t->note)));
            rows.push_back({{"name", t->name},
                            {"sport", t->sport},
                            {"is_national", t->national},
                            {"logo_url", t->national ? json(nullptr) : or_null(logo_url)},
                            {"flag_url", or_null(flag_url)},
                            {"country", or_null(t->country)},
                            {"wiki_title", or_null(t->wiki_title)}});
        }

        if (!problems.empty()) {
            std::cout << "\n" << problems.size() << " team(s) need a look (fix in team_logos/overrides.json):\n";
            for (const auto& p : problems) std::cout << "  - " << p << "\n";
        }
        if (dry_run) {
            std::cout << "\n--dry-run: nothing written\n";
        } else {
            for (const auto& batch : chunks(std::vector<json>(rows.begin(), rows.end()), 200))
                sb.upsert("teams", "name,sport", json(batch));
            std::cout << "\nUpserted " << rows.size() << " team(s) into `teams`\n";
        }
        curl_global_cleanup();
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "!! team_logos: " << e.what() << "\n";
        return 1;
    }
}
