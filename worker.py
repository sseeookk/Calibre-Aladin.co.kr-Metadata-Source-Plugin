#!/usr/bin/env python
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:ai
from __future__ import (unicode_literals, division, absolute_import, print_function)

__license__   = 'GPL v3'
__copyright__ = '2014, YongSeok Choi <sseeookk@gmail.com> based on the Goodreads work by Grant Drake <grant.drake@gmail.com>'
__docformat__ = 'restructuredtext en'

import socket, re, datetime, lxml
from collections import OrderedDict
from threading import Thread

from lxml.html import fromstring, tostring

from calibre.ebooks.metadata.book.base import Metadata
from calibre.library.comments import sanitize_comments_html
from calibre.utils.cleantext import clean_ascii_chars
from calibre.utils.localization import canonicalize_lang

import calibre_plugins.aladin_co_kr.config as cfg

class Worker(Thread): # Get details

    '''
    Get book details from Aladin book page in a separate thread
    '''

    def __init__(self, url, result_queue, browser, log, relevance, plugin, timeout=20):
        Thread.__init__(self)
        self.daemon = True
        self.url, self.result_queue = url, result_queue
        self.log, self.timeout = log, timeout
        self.relevance, self.plugin = relevance, plugin
        self.browser = browser.clone_browser()
        self.cover_url = self.aladin_id = self.isbn = None

        lm = {
                'eng': ('English', 'Englisch','ENG'),
                'zho': ('Chinese', 'chinois','chi'),
                'fra': ('French', 'Francais','FRA'),
                'ita': ('Italian', 'Italiano','ITA'),
                'dut': ('Dutch','DUT',),
                'deu': ('German', 'Deutsch','GER'),
                'spa': ('Spanish', 'Espa\xf1ol', 'Espaniol','SPA'),
                'jpn': ('Japanese', u'日本語','JAP'),
                'por': ('Portuguese', 'Portugues','POR'),
                'kor': ('Korean', u'한국어','KOR'),
                }
        self.lang_map = {}
        for code, names in lm.iteritems():
            for name in names:
                self.lang_map[name] = code

    def run(self):
        try:
            self.get_details()
        except:
            self.log.exception('get_details failed for url: %r'%self.url)

    def get_details(self):
        try:
            raw = self.browser.open_novisit(self.url, timeout=self.timeout).read().strip()
        except Exception as e:
            if callable(getattr(e, 'getcode', None)) and \
                    e.getcode() == 404:
                self.log.error('URL malformed: %r'%self.url)
                return
            attr = getattr(e, 'args', [None])
            attr = attr if attr else [None]
            if isinstance(attr[0], socket.timeout):
                msg = 'Aladin timed out. Try again later.'
                self.log.error(msg)
            else:
                msg = 'Failed to make details query: %r'%self.url
                self.log.exception(msg)
            return

        # raw = raw.decode('utf-8', errors='replace') #00

        # if '<title>404 - ' in raw:
            # self.log.error('URL malformed: %r'%self.url)
            # return

        try:
            root = fromstring(clean_ascii_chars(raw))
        except:
            msg = 'Failed to parse aladin details page: %r'%self.url
            self.log.exception(msg)
            return

        try:
            # Look at the <title> attribute for page to make sure that we were actually returned
            # a details page for a book. If the user had specified an invalid ISBN, then the results
            # page will just do a textual search.
            title_node = root.xpath('//title')
            if title_node:
                page_title = title_node[0].text_content().strip()
                
                # search success : '[알라딘]나의 문화유산답사기 1 - 남도답사 일번지, 개정판'
                # search fail : '[알라딘] "좋은 책을 고르는 방법, 알라딘"'
                if page_title is None or page_title.find('좋은 책을 고르는 방법, 알라딘') > -1:
                    self.log.error('Failed to see search results in page title: %r'%self.url)
                    return
        except:
            msg = 'Failed to read aladin page title: %r'%self.url
            self.log.exception(msg)
            return

        errmsg = root.xpath('//*[@id="errorMessage"]')
        if errmsg:
            msg = 'Failed to parse aladin details page: %r'%self.url
            msg += tostring(errmsg, method='text', encoding=unicode).strip()
            self.log.error(msg)
            return

        self.parse_details(root)

    def parse_details(self, root):
        try:
            aladin_id = self.parse_aladin_id(self.url)
        except:
            self.log.exception('Error parsing aladin id for url: %r'%self.url)
            aladin_id = None

        try:
            (title, series, series_index) = self.parse_title_series(root)
        except:
            self.log.exception('Error parsing title and series for url: %r'%self.url)
            title = series = series_index = None

        try:
            authors = self.parse_authors(root)
        except:
            self.log.exception('Error parsing authors for url: %r'%self.url)
            authors = []

        if not title or not authors or not aladin_id:
            self.log.error('Could not find title/authors/aladin id for %r'%self.url)
            self.log.error('aladin.co.kr: %r Title: %r Authors: %r'%(aladin_id, title,
                authors))
            return

        mi = Metadata(title, authors)
        if series:
            mi.series = series
            mi.series_index = series_index
        mi.set_identifier('aladin.co.kr', aladin_id)
        self.aladin_id = aladin_id

        try:
            isbn = self.parse_isbn(root)
            if isbn:
                self.isbn = mi.isbn = isbn
        except:
            self.log.exception('Error parsing ISBN for url: %r'%self.url)

        try:
            mi.rating = self.parse_rating(root)
        except:
            self.log.exception('Error parsing ratings for url: %r'%self.url)

        try:
            mi.comments = self.parse_comments(root)
        except:
            self.log.exception('Error parsing comments for url: %r'%self.url)

        try:
            self.cover_url = self.parse_cover(root)
        except:
            self.log.exception('Error parsing cover for url: %r'%self.url)
        mi.has_cover = bool(self.cover_url)

        try:
            tags = self.parse_tags(root)
            if tags:
                mi.tags = tags
                # mi['#mytags'] = "hello" # TODO: XXXX
        except:
            self.log.exception('Error parsing tags for url: %r'%self.url)

        try:
            mi.publisher, mi.pubdate = self.parse_publisher_and_date(root)
        except:
            self.log.exception('Error parsing publisher and date for url: %r'%self.url)

        try:
            lang = self._parse_language(root)
            if lang:
                mi.language = lang
        except:
            self.log.exception('Error parsing language for url: %r'%self.url)

        mi.source_relevance = self.relevance

        if self.aladin_id:
            if self.isbn:
                self.plugin.cache_isbn_to_identifier(self.isbn, self.aladin_id)
            if self.cover_url:
                self.plugin.cache_identifier_to_cover_url(self.aladin_id,
                        self.cover_url)

        self.plugin.clean_downloaded_metadata(mi)

        self.result_queue.put(mi)

    def parse_aladin_id(self, url):
        return re.search('wproduct\.aspx\?ItemId\=(.+)', url).groups(0)[0]

    def parse_title_series(self, root):
        title_node = root.xpath('//a[@class="p_topt01"]/..')
        if not title_node:
            return (None, None, None)
        series_node = title_node[0].xpath('.//a[contains(@href,"wseriesitem.aspx")]')
        if not series_node:
            title_text = title_node[0].text_content().strip()
            return (title_text, None, None)
        series_info = series_node[0].text_content().strip()
        
        # title에서 series 지우기 
        # 2016-02-03 안된다.
        series_node_parent = series_node[0].getparent()
        series_node_parent.getparent().remove(series_node_parent);
        
        title_text = title_node[0].text_content().strip()
        
        # 2016-02-03 추가
        title_text = re.sub('\l\s*' + series_info, "", title_text)
        title_text = title_text.strip()
        
        #title_text = title_node[0].text_content().strip()
        
        if series_info:
            match = re.search("\s+(\d+)\s*$",series_info)
            if match:
                series_index = match.group(1)
                series_name = series_info[:-1 * len(match.group(0))]
            else:
                series_index = 0
                series_name = series_info
                
            return (title_text, series_name, float(series_index))
        
        return (title_text, None, None)

    def parse_authors(self, root):
        # Build a dict of authors with their contribution if any in values
        authors_element = root.xpath('//a[@class="np_af" and contains(@href,"?AuthorSearch=")]/../child::node()')
        if not authors_element:
            return

        authors_type_map = OrderedDict()
        authors_element_len = len(authors_element)
        
        # 거꾸로 검색하면서 "(역할)"을 할당한다.
        contrib = ''
        for n in range(authors_element_len,0,-1):
            # print div_authors[n-1]
            el = authors_element[n-1]
            if isinstance(el, lxml.html.HtmlElement) and el.tag == "a" and re.search("AuthorSearch=", el.get("href")) and contrib:
                authors_type_map[el.text_content()] = contrib
            elif isinstance(el, lxml.etree._ElementUnicodeResult):
                match = re.search("\((.*)\)",el)
                if match:
                    contrib = match.group(1)
        
        item = authors_type_map.items()
        item.reverse()
        authors_type_map = OrderedDict(item)

        # User either requests all authors, or only the primary authors (latter is the default)
        # If only primary authors, only bring them in if:
        # 1. They have no author type specified
        # 2. They have an author type of 'Aladin Author'
        # 3. There are no authors from 1&2 and they have an author type of 'Editor'
        get_all_authors = cfg.plugin_prefs[cfg.STORE_NAME][cfg.KEY_GET_ALL_AUTHORS]
        authors = []
        valid_contrib = None
        for a, contrib in authors_type_map.iteritems():
            if get_all_authors:
                authors.append(a)
            else:
                if not contrib or contrib == u'지은이':
                    authors.append(a)
                elif len(authors) == 0:
                    authors.append(a)
                    valid_contrib = contrib
                elif contrib == valid_contrib:
                    authors.append(a)
                else:
                    break
        return authors

    def parse_rating(self, root):
        rating_node = root.xpath('//span[@class="star_nom"]')
        if rating_node:
            rating_value = int(float(rating_node[0].text_content())) / 2
            return rating_value

    def parse_comments(self, root):
        # <!-- 책소개-->
        # aladin uses other request for description and toc.
        # http://www.aladin.co.kr/shop/product/getContents.aspx?ISBN=8970122648&name=Introduce&type=0&date=16
        
        urlDesc = "http://www.aladin.co.kr/shop/product/getContents.aspx?ISBN=%s&name=Introduce&type=0&date=%s" % (self.isbn,datetime.datetime.now().hour)
        
        # TODO: foreign book description
        # 출판사 제공 책소개 - 외국 도서,
        # http://www.aladin.co.kr/shop/product/getContents.aspx?ISBN=0385340583&name=PublisherDesc&type=0&date=15
        
        comments = ''
        toc = ''
        
        try:
            self.browser.addheaders = [('Referer', self.url)]
            rawDesc = self.browser.open_novisit(urlDesc, timeout=self.timeout).read().strip()
        except Exception as e:
            if callable(getattr(e, 'getcode', None)) and e.getcode() == 404:
                self.log.error('URL malformed: %r' % urlDesc)
            else:
                attr = getattr(e, 'args', [None])
                attr = attr if attr else [None]
                if isinstance(attr[0], socket.timeout):
                    msg = 'Aladin timed out. Try again later.'
                    self.log.error(msg)
                else:
                    msg = 'Failed to make Descrpitions query: %r' % urlDesc
                    self.log.exception(msg)
                
        if rawDesc:
            try:
                #rawDesc = rawDesc.decode('euc-kr', errors='replace')
                # 2015-03-19 22:26:51
                rawDesc = rawDesc.decode('utf-8', errors='replace')
                rootDesc = fromstring(clean_ascii_chars(rawDesc))
                nodeDesc = rootDesc.xpath('//div[@class="p_textbox"]')
                if nodeDesc :
                    self._removeTags(nodeDesc[0],["object","script","style"])
                    comments = tostring(nodeDesc[0], method='html')
            except:
                msg = 'Failed to parse aladin details page: %r' % urlDesc
                self.log.exception(msg)
                
            default_append_toc = cfg.DEFAULT_STORE_VALUES[cfg.KEY_APPEND_TOC]
            append_toc = cfg.plugin_prefs[cfg.STORE_NAME].get(cfg.KEY_APPEND_TOC, default_append_toc)
        
            if rootDesc and append_toc:
                toc_node = rootDesc.xpath('//div[@id="div_TOC_All"]//p')
                if not toc_node:
                    toc_node = rootDesc.xpath('//div[@id="div_TOC_Short"]//p')
                if toc_node:
                    toc = tostring(toc_node[0], method='html')
                    toc = sanitize_comments_html(toc)
        if not comments:
            # Look for description in a meta
            description_node = root.xpath('//meta[@name="Description"]/@content')
            if description_node:
                # return description_node[0]
                comments = description_node[0]
        if comments:
            comments = '<div id="comments">' + comments + '</div>'
        if toc:
            comments += '<h3>[목차]</h3><div id="toc">' + toc + "</div>"
        if comments:
            comments_suffix = cfg.DEFAULT_STORE_VALUES[cfg.KEY_COMMENTS_SUFFIX]
            comments_suffix = cfg.plugin_prefs[cfg.STORE_NAME].get(cfg.KEY_COMMENTS_SUFFIX, comments_suffix)
            # comments += '<hr /><div><div style="float:right">[aladin.co.kr]</div></div>'
            if comments_suffix:
                comments += comments_suffix
        return comments

    def parse_cover(self, root):
        # http://image.aladin.co.kr/product/466/2/cover/8971460326_1.jpg
        # http://image.aladin.co.kr/product/466/2/letslook/8971460326_f.jpg
        # imgcol_node = root.xpath('//img[@id="mainCoverImg"]/@src') 
        # <meta property="og:image" content="http://image.aladin.co.kr/product/666/65/cover/898040932x_1.jpg"/>
        imgcol_node = root.xpath('//meta[@property="og:image"]/@content')
        
        if imgcol_node:
            img_url_small = imgcol_node[0]
            # aladin have no image.
            # http://image.aladin.co.kr/img/noimg_b.gif
            if "noimg" in img_url_small : return
            small_cover = cfg.DEFAULT_STORE_VALUES[cfg.KEY_SMALL_COVER]
            small_cover = cfg.plugin_prefs[cfg.STORE_NAME].get(cfg.KEY_SMALL_COVER, small_cover)
            if small_cover:
                img_url = img_url_small
            else:
                img_url = re.sub("/cover/","/letslook/",img_url_small)
                img_url = re.sub("_\d.jpg","_f.jpg",img_url)
                img_url = re.sub("_\d.gif","_f.jpg",img_url)
            try:
                # Unfortunately Aladin sometimes have broken links so we need to do
                # an additional request to see if the URL actually exists
                info = self.browser.open_novisit(img_url, timeout=self.timeout).info()
                if int(info.getheader('Content-Length')) > 1000:
                    return img_url
                else:
                    self.log.warning('Broken image for url: %s'%img_url)
            except:
                pass
    
    
    def parse_isbn(self, root):
        isbn_node = root.xpath('//div[@class="p_goodstd03"]')
        if isbn_node:
            match = re.search("isbn(?:\(13\))?\s?:\s?([^\s]*)",isbn_node[0].text_content(),re.I)
            if match:
                return match.group(1)

    def parse_publisher_and_date(self, root):
        # Publisher is specified within the a :
        #  <a class="np_af" href="/search/wsearchresult.aspx?PublisherSearch=%b4%d9%b9%ae@876&BranchType=1">다문</a> | 2009-09-20
        publisher = None
        pub_date = None
        publisher_node = root.xpath('//a[@class="np_af" and contains(@href,"?PublisherSearch=")]')
        if publisher_node:
            publisher = publisher_node[0].text_content()

            # Now look for the pubdate. There should always be one at start of the string
            pubdate_text_str = publisher_node[0].tail
            if pubdate_text_str :
                pubdate_text_match = re.search('(\d{4}-\d{1,2}-\d{1,2})', pubdate_text_str)
                if pubdate_text_match is not None:
                    pubdate_text = pubdate_text_match.groups(0)[0]
                    if pubdate_text:
                        pub_date = self._convert_date_text_hyphen(pubdate_text)
        return (publisher, pub_date)

    def parse_tags(self, root):
        # Aladin have both"tags" and Genres(category)
        # We will use those as tags (with a bit of massaging)
        
        calibre_tags = list()
        
        aladin_category_lookup = cfg.plugin_prefs[cfg.STORE_NAME][cfg.KEY_GET_CATEGORY]
        
        if aladin_category_lookup:
            genres_node = root.xpath('//div[@class="p_categorize"]/ul/li')
            #self.log.info("Parsing categories")
            if genres_node:
                #self.log.info("Found genres_node")
                for genre in genres_node:
                    genre = genre.text_content().strip() 
                    # &nbsp; 를 공란(space)로 변환
                    genre = re.sub("\xc2?\xa0"," ",genre)
                    genre = re.sub("^\s*(국내도서|외국도서)\s*>\s*","",genre)
                    category_prefix = cfg.DEFAULT_STORE_VALUES[cfg.KEY_CATEGORY_PREFIX]
                    category_prefix = cfg.plugin_prefs[cfg.STORE_NAME].get(cfg.KEY_CATEGORY_PREFIX, category_prefix)
                    if category_prefix:
                        calibre_tags.append(category_prefix + ".".join(re.split("\s*\>\s*",genre)))
                    else:
                        calibre_tags.append(".".join(re.split("\s*\>\s*",genre)))
                
                
        tags_list = root.xpath('//div[@id="div_itemtaglist"]//a[contains(@href,"tagname=")]/text()')
        #self.log.info("Parsing tags")
        if tags_list:
            #self.log.info("Found tags")
            convert_tag_lookup = cfg.plugin_prefs[cfg.STORE_NAME][cfg.KEY_CONVERT_TAG]
            if convert_tag_lookup:
                tags = self._convert_genres_to_calibre_tags(tags_list)
            else:
                tags = tags_list
            if len(tags) > 0:
                # return calibre_tags
                calibre_tags.extend(tags)
                
        return calibre_tags

    def _convert_genres_to_calibre_tags(self, genre_tags):
        # for each tag, add if we have a dictionary lookup
        calibre_tag_lookup = cfg.plugin_prefs[cfg.STORE_NAME][cfg.KEY_GENRE_MAPPINGS]
        calibre_tag_map = dict((k.lower(),v) for (k,v) in calibre_tag_lookup.iteritems())
        tags_to_add = list()
        for genre_tag in genre_tags:
            tags = calibre_tag_map.get(genre_tag.lower(), None)
            if tags:
                for tag in tags:
                    if tag not in tags_to_add:
                        tags_to_add.append(tag)
        # return list(tags_to_add)
        return tags_to_add

    def _convert_date_text(self, date_text):
        # Note that the date text could be "2003", "December 2003" or "December 10th 2003"
        year = int(date_text[-4:])
        month = 1
        day = 1
        if len(date_text) > 4:
            text_parts = date_text[:len(date_text)-5].partition(' ')
            month_name = text_parts[0]
            # Need to convert the month name into a numeric value
            # For now I am "assuming" the Aladin website only displays in English
            # If it doesn't will just fallback to assuming January
            month_dict = {"January":1, "February":2, "March":3, "April":4, "May":5, "June":6,
                "July":7, "August":8, "September":9, "October":10, "November":11, "December":12}
            month = month_dict.get(month_name, 1)
            if len(text_parts[2]) > 0:
                day = int(re.match('([0-9]+)', text_parts[2]).groups(0)[0])
        from calibre.utils.date import utc_tz
        return datetime.datetime(year, month, day, tzinfo=utc_tz)

    def _convert_date_text_hyphen(self, date_text):
        # 2014-03-09 to datetime
        year = 2014
        month = 1
        day = 1
        dates = date_text.split('-')

        if len(dates) >= 1: 
            year = int(dates[0])

        if len(dates) >= 2: 
            month = int(dates[1])

        if len(dates) >= 3: 
            day = int(dates[2])

        from calibre.utils.date import utc_tz
        return datetime.datetime(year, month, day, tzinfo=utc_tz)

    # Defalut language is Korean at Aladin. 
    # Aladin 에서 언어를 찾을 수 없을 때
    # 기본 언어로 Korean 을 넣는다.
    def _parse_language(self, root):
        raw = "Korean"
        lang_node = root.xpath('//div[@class="p_goodstd03"]')
        if lang_node:
            match = re.search("%s\s?:\s?([^\s]*)" % u'언어',lang_node[0].text_content(),re.I)
            if match:
                raw = match.group(1)
        ans = self.lang_map.get(raw, None)
        if ans:
            return ans
        ans = canonicalize_lang(ans)
        if ans:
            return ans

    def _removeTags(self, element, tags):
        try:
            for node in element.getchildren():
                if node.tag in tags:
                    element.remove(node)
                else:
                    self._removeTags(node,tags)
        except:
            return
            
