#!/usr/bin/env python
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:ai
from __future__ import (unicode_literals, division, absolute_import, print_function)

import datetime
import lxml
import re
import socket
from collections import OrderedDict
from threading import Thread

import calibre_plugins.aladin_co_kr.config as cfg
from calibre.ebooks.metadata.book.base import Metadata
from calibre.library.comments import sanitize_comments_html
from calibre.utils.cleantext import clean_ascii_chars
from calibre.utils.localization import canonicalize_lang
from lxml.html import fromstring, tostring

from six import text_type as unicode


__license__   = 'GPL v3'
__copyright__ = '2014, YongSeok Choi <sseeookk@gmail.com> based on the Goodreads work by Grant Drake <grant.drake@gmail.com>'
__docformat__ = 'restructuredtext en'


class Worker(Thread):  # Get details
    """
    Get book details from Aladin book page in a separate thread
    """
    
    def __init__(self, url, result_queue, browser, log, relevance, plugin, timeout=20):
        Thread.__init__(self)
        self.daemon = True
        self.url, self.result_queue = url, result_queue
        self.log, self.timeout = log, timeout
        self.relevance, self.plugin = relevance, plugin
        self.browser = browser.clone_browser()
        self.cover_url = self.aladin_id = self.isbn = None
        
        lm = {
            'eng': ('English', 'Englisch', 'ENG'),
            'zho': ('Chinese', 'chinois', 'chi'),
            'fra': ('French', 'Francais', 'FRA'),
            'ita': ('Italian', 'Italiano', 'ITA'),
            'dut': ('Dutch', 'DUT',),
            'deu': ('German', 'Deutsch', 'GER'),
            'spa': ('Spanish', 'Espa\xf1ol', 'Espaniol', 'SPA'),
            'jpn': ('Japanese', u'日本語', 'JAP'),
            'por': ('Portuguese', 'Portugues', 'POR'),
            'kor': ('Korean', u'한국어', 'KOR'),
        }
        self.lang_map = {}
        for code, names in lm.items():
            for name in names:
                self.lang_map[name] = code
    
    def run(self):
        try:
            self.get_details()
        except:
            self.log.exception('get_details failed for url: %r' % self.url)
    
    def get_details(self):
        try:
            raw = self.browser.open_novisit(self.url, timeout=self.timeout).read().strip()
        except Exception as e:
            if callable(getattr(e, 'getcode', None)) and \
                    e.getcode() == 404:
                self.log.error('URL malformed: %r' % self.url)
                return
            attr = getattr(e, 'args', [None])
            attr = attr if attr else [None]
            if isinstance(attr[0], socket.timeout):
                msg = 'Aladin timed out. Try again later.'
                self.log.error(msg)
            else:
                msg = 'Failed to make details query: %r' % self.url
                self.log.exception(msg)
            return
        
        raw = raw.decode('utf-8', errors='replace')  # 00
        # raw = raw.decode('euc-kr', 'ignore')  # sseeookk python2
        
        # if '<title>404 - ' in raw:
        # self.log.error('URL malformed: %r'%self.url)
        # return
        
        try:
            root = fromstring(clean_ascii_chars(raw))
        except:
            msg = 'Failed to parse aladin details page: %r' % self.url
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
                    self.log.error('Failed to see search results in page title: %r' % self.url)
                    return
        except:
            msg = 'Failed to read aladin page title: %r' % self.url
            self.log.exception(msg)
            return
        
        errmsg = root.xpath('//*[@id="errorMessage"]')
        if errmsg:
            msg = 'Failed to parse aladin details page: %r' % self.url
            msg += tostring(errmsg, method='text', encoding=unicode).strip()
            self.log.error(msg)
            return
        
        self.parse_details(root)
    
    def parse_details(self, root):
        try:
            aladin_id = self.parse_aladin_id(self.url, root)
        except:
            self.log.exception('Error parsing aladin id for url: %r' % self.url)
            aladin_id = None
        
        try:
            (title, series, series_index) = self.parse_title_series(root)
        except:
            self.log.exception('Error parsing title and series for url: %r' % self.url)
            title = series = series_index = None
        
        try:
            authors = self.parse_authors(root)
        except:
            self.log.exception('Error parsing authors for url: %r' % self.url)
            authors = []
        
        if not title or not authors or not aladin_id:
            self.log.error('Could not find title/authors/aladin id for %r' % self.url)
            self.log.error('aladin.co.kr: %r Title: %r Authors: %r' % (aladin_id, title, authors))
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
            self.log.exception('Error parsing ISBN for url: %r' % self.url)
        
        try:
            mi.rating = self.parse_rating(root)
        except:
            self.log.exception('Error parsing ratings for url: %r' % self.url)
        
        try:
            mi.comments = self.parse_comments(root)
        except:
            self.log.exception('Error parsing comments for url: %r' % self.url)
        
        try:
            self.cover_url = self.parse_cover(root)
        except:
            self.log.exception('Error parsing cover for url: %r' % self.url)
        mi.has_cover = bool(self.cover_url)
        
        try:
            tags = self.parse_tags(root)
            if tags:
                mi.tags = tags
        except:
            self.log.exception('Error parsing tags for url: %r' % self.url)
        
        try:
            mi.publisher, mi.pubdate = self.parse_publisher_and_date(root)
        except:
            self.log.exception('Error parsing publisher and date for url: %r' % self.url)
        
        try:
            lang = self._parse_language(root)
            if lang:
                mi.language = lang
        except:
            self.log.exception('Error parsing language for url: %r' % self.url)
        
        mi.source_relevance = self.relevance
        
        if self.aladin_id:
            if self.isbn:
                self.plugin.cache_isbn_to_identifier(self.isbn, self.aladin_id)
            if self.cover_url:
                self.plugin.cache_identifier_to_cover_url(self.aladin_id, self.cover_url)
        # self.log.info(mi)
        self.plugin.clean_downloaded_metadata(mi)
        
        self.result_queue.put(mi)
    
    def parse_aladin_id(self, url, root):
        # 'https://www.aladin.co.kr/shop/wproduct.aspx?ItemId=%d'
        match = re.search(r'wproduct\.aspx\?ItemId=(.+)', url)
        if match:
            return match.group(1)

        # <meta property="og:url" content="https://www.aladin.co.kr/shop/wproduct.aspx?ItemId=125451796" />
        page_url = root.xpath('//meta[@property="og:url"]')[0].attrib['content']
        return re.search(r'wproduct\.aspx\?ItemId=(.+)', page_url).group(1)
    
    def parse_title_series(self, root):
        # title_node = root.xpath('//a[@class="p_topt01"]/..') <div><a
        # href="https://www.aladin.co.kr/shop/wproduct.aspx?ItemId=125451796" class="Ere_bo_title">[eBook] Head First
        # Python (개정판)</a> <span class="Ere_sub1_title">- 스스로 질문하며 답을 찾는 파이썬 학습서(Python 3), 개정판</span> <a
        # href="/search/wsearchresult.aspx?SearchTarget=All&amp;SearchWord=9791162249857"><img
        # src="//image.aladin.co.kr/img/shop/2018/icon_top_search.png" style="margin-bottom:-4px;"></a>
        # | <a href="/shop/common/wseriesitem.aspx?SRID=111846" class="Ere_sub1_title Ere_sub_blue">Head First 시리즈 3</a>
        # ......
        # </div>
        title_node = root.xpath('//a[@class="Ere_bo_title"]/..')
        if not title_node:
            return None, None, None
        title_text = title_node[0].text_content().strip()
        
        series_node = title_node[0].xpath('.//a[contains(@href,"wseriesitem.aspx")]')
        if not series_node:
            return title_text, None, None
        series_info = series_node[0].text_content().strip()
        
        # title에서 series 지우기 
        # 2016-02-03 안된다.
        series_node_parent = series_node[0].getparent()
        series_node_parent.getparent().remove(series_node_parent)
        
        # 2016-02-03 추가
        # title_text = re.sub('\l\s*' + series_info, "", title_text)
        title_text = re.sub(r'\|\s*' + series_info, "", title_text)
        title_text = title_text.strip()
        
        if series_info:
            match = re.search(r"\s+(\d+)\s*$", series_info)
            if match:
                series_index = match.group(1)
                series_name = series_info[:-1 * len(match.group(0))]
            else:
                series_index = 0
                series_name = series_info
            
            return title_text, series_name, float(series_index)
        
        return title_text, None, None
    
    def parse_authors(self, root):
        # # Build a dict of authors with their contribution if any in values
        # # authors_element = root.xpath('//a[@class="np_af" and contains(@href,"?AuthorSearch=")]/../child::node()')
        # authors_element = root.xpath('//div[@class="tlist"]//a[contains(@href, "AuthorSearch=")]/../child::node()')
        # if not authors_element:
        #     return
        #
        # authors_type_map = OrderedDict()
        # authors_element_len = len(authors_element)
        #
        # # 거꾸로 검색하면서 "(역할)"을 할당한다.
        # contrib = ''
        # for n in range(authors_element_len, 0, -1):
        #     # print div_authors[n-1]
        #     el = authors_element[n - 1]
        #     if isinstance(el, lxml.html.HtmlElement) \
        #             and el.tag == "a" \
        #             and re.search("AuthorSearch=", el.get("href")) \
        #             and contrib:
        #         authors_type_map[el.text_content()] = contrib
        #     elif isinstance(el, lxml.etree._ElementUnicodeResult):
        #         match = re.search("\((.*)\)", el)
        #         if match:
        #             contrib = match.group(1)
        #
        # item = authors_type_map.items()
        # self.log.debug("authors_type_map ==============")
        # self.log.debug(authors_type_map)
        # self.log.debug(item)
        # item.reverse()
        # authors_type_map = OrderedDict(item)
        # self.log.debug("authors_type_map ==============")
        # self.log.debug(authors_type_map)
        #
        # # User either requests all authors, or only the primary authors (latter is the default)
        # # If only primary authors, only bring them in if:
        # # 1. They have no author type specified
        # # 2. They have an author type of 'Aladin Author'
        # # 3. There are no authors from 1&2 and they have an author type of 'Editor'
        # get_all_authors = cfg.plugin_prefs[cfg.STORE_NAME][cfg.KEY_GET_ALL_AUTHORS]
        # authors = []
        # valid_contrib = None
        # for a, contrib in authors_type_map.items():
        #     if get_all_authors:
        #         authors.append(a)
        #     else:
        #         if not contrib or contrib == u'지은이':
        #             authors.append(a)
        #         elif len(authors) == 0:
        #             authors.append(a)
        #             valid_contrib = contrib
        #         elif contrib == valid_contrib:
        #             authors.append(a)
        #         else:
        #             break
        # return authors
        
        default_get_all_authors = cfg.DEFAULT_STORE_VALUES[cfg.KEY_GET_ALL_AUTHORS]
        get_all_authors = cfg.plugin_prefs[cfg.STORE_NAME].get(cfg.KEY_GET_ALL_AUTHORS, default_get_all_authors)

        author_nodes = root.xpath('//div[@class="tlist"]//a[contains(@href, "AuthorSearch=")]')
        if author_nodes:
            authors = []
            
            for author_node in author_nodes:
                author = author_node.text.strip()
                if author:
                    authors.append(author)
                if get_all_authors:
                    # (지은이)나 (엮은이) 등과 같은 textnode가 바로 뒤에 나오면 authors에 그만 더하고 for를 끝낸다.
                    match = re.search(r"\(.*\)", author_node.tail)
                    if match:
                        break
            
            return authors
    
    def parse_rating(self, root):
        # rating_node = root.xpath('//span[@class="star_nom"]')
        
        # # <a href="javascript:void(0);" onclick="showRankLayer();return false;"
        # # class="Ere_sub_pink Ere_fs16 Ere_str">8.7 </a>
        # rating_nodes = root.xpath('//div[@class="info_list"]//a[contains(@onclick,"showRankLayer()")]')
        #
        # if rating_nodes:
        #     # rating_value = int(float(rating_nodes[0].text_content().strip())) / 2
        #     # rating_value = rating_nodes[0].text_content().strip()
        #     self.log.debug(rating_nodes)
        #     self.log.debug(rating_nodes[0])
        #     rating_value = rating_nodes[0].text_content().strip()
        #     self.log.debug("rating_value???????" + rating_value)
        #     if rating_value:
        #         return float(rating_value) / 2
        
        # <a href="javascript:void(0);" onclick="showRankLayer();return false;">
        # <img src="//image.aladin.co.kr/img/shop/2012/icon_star8.png" style="margin-bottom:-4px;"> </a>
        # <a href="javascript:void(0);" onclick="showRankLayer();return false;"
        # class="Ere_sub_pink Ere_fs16 Ere_str">8.7 </a>
        # 별 이미지와 숫자가 같이 있다.
        # 그래서 숫자가 있는 것을 고르기 위해 for 구문
        rating_nodes = root.xpath('//div[@class="info_list"]//a[contains(@onclick,"showRankLayer()")]/text()')
        if rating_nodes:
            for rating_node in rating_nodes:
                rating_value = rating_node.strip()
                if rating_value:
                    return float(rating_value) / 2
    
    def parse_comments(self, root):
        # 2021-06-24
        # <!-- 책소개-->
        # aladin uses other request for description and toc.
        # 국내 도서 : 책소개
        # http://www.aladin.co.kr/shop/product/getContents.aspx?ISBN=8970122648&name=Introduce&type=0&date=16
        # 외국 도서 : 출판사 제공 책소개"
        # https://www.aladin.co.kr/shop/product/getContents.aspx?ISBN=1491919531&name=PublisherDesc&type=0&date=15
        
        names = ['Introduce', 'PublisherDesc']

        comments = ''
        toc = ''
        rawDesc = ''
        urlDesc = ''
        
        for name in names:
            urlDesc = "http://www.aladin.co.kr/shop/product/getContents.aspx?ISBN=%s&name=%s&type=0&date=%s" %\
                      (self.isbn, name, datetime.datetime.now().hour)
            
            try:
                self.browser.addheaders = [('Referer', self.url)]
                rawDesc = self.browser.open_novisit(urlDesc, timeout=self.timeout).read().strip()
                
                if len(rawDesc) > 0:
                    break
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
            rootDesc = None
            try:
                rawDesc = rawDesc.decode('utf-8', errors='replace')
                rootDesc = fromstring(clean_ascii_chars(rawDesc))
                
                # # rawDesc = rawDesc.decode('euc-kr', errors='replace')
                # # 2015-03-19 22:26:51
                # nodeDesc = rootDesc.xpath('//div[@class="p_textbox"]')
                # if nodeDesc:
                #     self._removeTags(nodeDesc[0], ["object", "script", "style"])
                #     comments = tostring(nodeDesc[0], method='html')
                
                # 2021-06-24
                # <!-- 책소개-->
                #
                #     <a id="8931463375_introduce"></a>
                #     <div class="Ere_prod_mconts_box">
                #
                #         <div class="Ere_prod_mconts_LS">책소개</div>
                #         <div class="Ere_prod_mconts_LL">책소개</div>
                #         <div class="Ere_prod_mconts_R">
                introduce_nodes = rootDesc.xpath(
                    './/div[@class="Ere_prod_mconts_box"]//div[text()="책소개"]/..//div[@class="Ere_prod_mconts_R"]')
                
                # 2021-06-24
                # <!-- 출판사 제공 책소개 start-->
                # <div class="Ere_prod_mconts_box">
                #
                #     <div class="Ere_prod_mconts_LS">출판사 제공 책소개</div>
                #     <div class="Ere_prod_mconts_LL">출판사 제공<br>책소개</div>
                #     <div class="Ere_prod_mconts_R">
                #         <!-- 책소개 이미지 -->
                #
                #         <div style="word-break:break-all;"><div><p>......</p></div></div>
                #         <div class="Ere_line2"></div>
                #     </div>
                #     <div class="Ere_clear"></div>
                # </div>
                # <!-- 출판사 제공 책소개 end-->
                if not introduce_nodes:
                    introduce_nodes = rootDesc.xpath(
                        './/div[@class="Ere_prod_mconts_box"]//div[text()="출판사 제공 책소개"]/..'
                        '//div[@class="Ere_prod_mconts_R"]')
                
                if introduce_nodes:
                    # self.log('Got a comments description node')
                    self._removeTags(introduce_nodes[0], ["object", "script", "style"])
                    # comments = tostring(introduce_nodes[0], method='html', encoding=six.text_type).strip()
                    comments = tostring(introduce_nodes[0], method='html').strip()
                    # self.log('Raw comments:',comments)
                    comments = sanitize_comments_html(comments)
                    comments = comments.replace('<h2>Overview</h2>', '')
                    # open('E:\\aladin_comments.html', 'wb').write(comments)
            except:
                msg = 'Failed to parse aladin details page: %r' % urlDesc
                self.log.exception(msg)
            
            default_append_toc = cfg.DEFAULT_STORE_VALUES[cfg.KEY_APPEND_TOC]
            append_toc = cfg.plugin_prefs[cfg.STORE_NAME].get(cfg.KEY_APPEND_TOC, default_append_toc)
            
            #     <!-- 목차 시작 -->
            #     <div class="Ere_prod_mconts_box">
            #         <div class="Ere_prod_mconts_LL">목차</div>
            #         <div class="Ere_prod_mconts_LS">목차</div>
            #         <div class="Ere_prod_mconts_R" id="tocTemplate">
            #             <div id="div_TOC_Short" style="word-break: break-all">
            #             <a href="javascript:fn_show_introduce_TOC('TOC')"><p><B>0장 도입</B>
            if rootDesc is not None and append_toc:
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
        
        # 2021-06-24
        # <meta property="og:image" content="https://image.aladin.co.kr/product/1358/21/cover500/8979148682_1.jpg"/>
        # https://image.aladin.co.kr/product/1358/21/cover/8979148682_1.jpg  # 200 x 257
        # https://image.aladin.co.kr/product/1358/21/cover500/8979148682_1.jpg  # 500 x 644
        # https://image.aladin.co.kr/product/1358/21/letslook/8979148682_f.jpg  # 없는 경우가 있다.
        # no image:
        # <meta property="og:image" content="https://image.aladin.co.kr/img/shop/2018/img_no.jpg"/>
        
        imgcol_node = root.xpath('//meta[@property="og:image"]/@content')
        
        if imgcol_node:
            img_url_small = imgcol_node[0]
            # aladin have no image.
            # http://image.aladin.co.kr/img/noimg_b.gif
            if "noimg" in img_url_small or "img_no.jpg" in img_url_small:
                return
            small_cover = cfg.DEFAULT_STORE_VALUES[cfg.KEY_SMALL_COVER]
            small_cover = cfg.plugin_prefs[cfg.STORE_NAME].get(cfg.KEY_SMALL_COVER, small_cover)
            if small_cover:
                # img_url = img_url_small
                img_url = re.sub(r"/cover\d*/", "/cover/", img_url_small)
            else:
                # img_url = re.sub("/cover/", "/letslook/", img_url_small)
                # img_url = re.sub(r"/cover\d*/", "/letslook/", img_url_small)
                img_url = re.sub("/cover/", "/cover500/", img_url_small)
                # img_url = re.sub(r"_\d.jpg", "_f.jpg", img_url)
                # img_url = re.sub(r"_\d.gif", "_f.jpg", img_url)
            
            try:
                # Unfortunately Aladin sometimes have broken links so we need to do
                # an additional request to see if the URL actually exists
                info = self.browser.open_novisit(img_url, timeout=self.timeout).info()
                # if int(info.getheader('Content-Length')) > 1000:  # Python 2
                if int(info.get('Content-Length')) > 1000:  # Python 3
                    return img_url
                else:
                    self.log.warning('Broken image for url: %s' % img_url)
            except:
                # self.log.info(e)
                self.log.info('parse_cover error!')
                pass
    
    def parse_isbn(self, root):
        # isbn_nodes = root.xpath('//div[@class="p_goodstd03"]')
        # for isbn_node in isbn_nodes:
        #     match = re.search("isbn(?:\(13\))?\s?:\s?([^\s]*)", isbn_node.text_content(), re.I)
        #     if match:
        #         return match.group(1)
        
        # <meta property="books:isbn" content="9791162240281" />
        isbn_nodes = root.xpath('//meta[@property="books:isbn"]')
        if isbn_nodes:
            return isbn_nodes[0].attrib['content']
    
    def parse_publisher_and_date(self, root):
        # Publisher is specified within the a :
        publisher = None
        pub_date = None

        # #  <a class="np_af" href="/search/wsearchresult.aspx?PublisherSearch=%b4%d9%b9%ae@876&BranchType=1">다문</a>
        # # | 2009-09-20
        # publisher_node = root.xpath('//a[@class="np_af" and contains(@href,"?PublisherSearch=")]')
        #
        # if publisher_node:
        #     publisher = publisher_node[0].text_content()
        #
        #     # Now look for the pubdate. There should always be one at start of the string
        #     pubdate_text_str = publisher_node[0].tail
        #     if pubdate_text_str:
        #         pubdate_text_match = re.search(r'(\d{4}-\d{1,2}-\d{1,2})', pubdate_text_str)
        #         if pubdate_text_match is not None:
        #             pubdate_text = pubdate_text_match.groups(0)[0]
        #             if pubdate_text:
        #                 pub_date = self._convert_date_text_hyphen(pubdate_text)
        # return publisher, pub_date

        # <li class="Ere_sub2_title"><a href="/search/wsearchresult.aspx?AuthorSearch=%ed%8f%b4+%eb%b2%a0%eb%a6%ac
        # @2199859&amp;BranchType=1" class="Ere_sub2_title">폴 베리</a>&nbsp;(지은이),<span class="Ere_PR10"></span>
        # <a href="/search/wsearchresult.aspx?AuthorSearch=%ea%b0%95%ea%b6%8c%ed%95%99@1668244&amp;BranchType=1"
        # class="Ere_sub2_title">강권학</a>&nbsp;(옮긴이)<span class="Ere_PR10"></span>
        # <a class="Ere_sub2_title"
        # href="/search/wsearchresult.aspx?PublisherSearch=%ed%95%9c%eb%b9%9b%eb%af%b8%eb%94%94%ec%96%b4@6555&amp
        # ;BranchType=1">한빛미디어</a><span class="Ere_PR10"></span>2011-10-28
        # <span class="Ere_PR10"></span><a
        # class="Ere_sub2_title" href="/search/wsearchresult.aspx?SearchTarget=Foreign&amp;SearchWord=Head+First
        # +Python%2c+First+Edition+Paul+Barry">원제 : Head First Python, First Edition</a></li>
        publisher_node = root.xpath('//div[@class="tlist"]//a[contains(@href, "PublisherSearch=")]')
        
        if publisher_node:
            publisher = publisher_node[0].text_content()
        
        # <meta itemprop="datePublished" content="2017-12-04">
        pub_date_nodes = root.xpath('//meta[@itemprop="datePublished"]/@content')
        if pub_date_nodes:
            pub_date = self._convert_date_text_hyphen(pub_date_nodes[0])
        
        return publisher, pub_date
    
    def parse_tags(self, root):
        # Aladin have both"tags" and Genres(category)
        # We will use those as tags (with a bit of massaging)
        
        calibre_tags = list()
        
        aladin_category_lookup = cfg.plugin_prefs[cfg.STORE_NAME][cfg.KEY_GET_CATEGORY]
        
        # 2021-06-24
        # tag가 없고 카테코리(주제분류)만 있다.
        # <ul id="ulCategory">
        #     <li>
        #     <a href="/home/wforeignmain.aspx">외국도서</a>&nbsp;&gt;&nbsp;
        #     <a href="/shop/wbrowse.aspx?CID=90859">컴퓨터</a>&nbsp;&gt;&nbsp;
        #     <a href="/shop/wbrowse.aspx?CID=97942">프로그래밍 언어</a>&nbsp;&gt;&nbsp;
        #     <a href="/shop/wbrowse.aspx?CID=105061&amp;BrowseTarget=List"><b>Python</b></a>
        #     <a href="javascript:fn_ForeignCategoryToggle();" class="Ere_sub_gray8 Ere_fs13 foreignCategoryFold"
        #     style="display:none;">접기
        #     <img src="//image.aladin.co.kr/img/shop/2018/icon_arrow_fold.png" style="margin-bottom:-1px;"></a>
        #     </li>
        #     ......
        #     <li class="foreignCategory" style="display:none;">
        #     <a href="/home/wforeignmain.aspx">외국도서</a>&nbsp;&gt;&nbsp;
        #     <a href="/shop/wbrowse.aspx?CID=90859">컴퓨터</a>&nbsp;&gt;&nbsp;
        #     <a href="/shop/wbrowse.aspx?CID=97942">프로그래밍 언어</a>&nbsp;&gt;&nbsp;
        #     <a href="/shop/wbrowse.aspx?CID=105045&amp;BrowseTarget=List"><b>일반</b></a>
        #     <a href="javascript:fn_ForeignCategoryToggle();" class="Ere_sub_gray8 Ere_fs13 foreignCategoryFold"
        #     style="display:none;">접기
        #     <img src="//image.aladin.co.kr/img/shop/2018/icon_arrow_fold.png" style="margin-bottom:-1px;"></a>
        #     </li>
        # </ul>
        
        if aladin_category_lookup:
            # genres_node = root.xpath('//div[@class="p_categorize"]/ul/li')
            
            # 2021-06-24
            genres_node = root.xpath('//ul[@id="ulCategory"]/li')
            
            # self.log.info("Parsing categories")
            if genres_node:
                # self.log.info("Found genres_node")
                for genre in genres_node:
                    for bad in genre.xpath("./a[text()='접기']"):
                        genre.remove(bad)
                    genre = genre.text_content().strip()
                    # &nbsp; 를 공란(space)로 변환
                    genre = re.sub(r"\xc2?\xa0", " ", genre)
                    genre = re.sub(r"^\s*(국내도서|외국도서)\s*>\s*", "", genre)
                    category_prefix = cfg.DEFAULT_STORE_VALUES[cfg.KEY_CATEGORY_PREFIX]
                    category_prefix = cfg.plugin_prefs[cfg.STORE_NAME].get(cfg.KEY_CATEGORY_PREFIX, category_prefix)
                    if category_prefix:
                        calibre_tags.append(category_prefix + ".".join(re.split(r"\s*>\s*", genre)))
                    else:
                        calibre_tags.append(".".join(re.split(r"\s*>\s*", genre)))
        
        # tags_list = root.xpath('//div[@id="div_itemtaglist"]//a[contains(@href,"tagname=")]/text()')
        #
        # # self.log.info("Parsing tags")
        # if tags_list:
        #     # self.log.info("Found tags")
        #     convert_tag_lookup = cfg.plugin_prefs[cfg.STORE_NAME][cfg.KEY_CONVERT_TAG]
        #     if convert_tag_lookup:
        #         tags = self._convert_genres_to_calibre_tags(tags_list)
        #     else:
        #         tags = tags_list
        #     if len(tags) > 0:
        #         # return calibre_tags
        #         calibre_tags.extend(tags)
        
        # 2021-06-24
        # 카테고리를 쓰지 않은 경우만 카테고리를 따로 떼어 태그로 쓴다.
        convert_tag_lookup = cfg.plugin_prefs[cfg.STORE_NAME][cfg.KEY_CONVERT_TAG]
        tags_list = None
        if not aladin_category_lookup:
            # tags_list = root.xpath('//ul[@id="ulCategory"]/li//a[contains(@href,"wbrowse.aspx?CID=")]/text()')
            tags_list = root.xpath('//ul[@id="ulCategory"]/li//a[contains(@href,"wbrowse.aspx?CID=")]')
            # tags_list = root.xpath('string(//ul[@id="ulCategory"]/li//a[contains(@href,"wbrowse.aspx?CID=")])')
            
            # self.log.info("Parsing tags")
            if tags_list:
                # self.log.info("Found tags")
                for tag_node in tags_list:
                    # tags_ = [tag_node]
                    tags_ = [tag_node.text_content().strip()]
                    
                    if convert_tag_lookup:
                        tags = self._convert_genres_to_calibre_tags(tags_)
                    else:
                        tags = tags_
                    if tags[0] in calibre_tags:
                        continue
                    if len(tags) > 0:
                        # return calibre_tags
                        calibre_tags.extend(tags)
        self.log.info("calibre_tags --------------------------------")
        self.log.info(calibre_tags)
        self.log.info(len(calibre_tags))
        
        return calibre_tags
    
    def _convert_genres_to_calibre_tags(self, genre_tags):
        # for each tag, add if we have a dictionary lookup
        calibre_tag_lookup = cfg.plugin_prefs[cfg.STORE_NAME][cfg.KEY_GENRE_MAPPINGS]
        calibre_tag_map = dict((k.lower(), v) for (k, v) in calibre_tag_lookup.items())
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
            text_parts = date_text[:len(date_text) - 5].partition(' ')
            month_name = text_parts[0]
            # Need to convert the month name into a numeric value
            # For now I am "assuming" the Aladin website only displays in English
            # If it doesn't will just fallback to assuming January
            month_dict = {"January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
                          "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12}
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
        
        # lang_node = root.xpath('//div[@class="p_goodstd03"]')
        # if lang_node:
        #     match = re.search(r"%s\s?:\s?([^\s]*)" % u'언어', lang_node[0].text_content(), re.I)
        #     if match:
        #         raw = match.group(1)
        
        # 2021-06-24
        lang_node = root.xpath('//div[@class="conts_info_list1"]//li[text()="언어 : "]/b')
        if lang_node:
            raw = lang_node[0].text_content()
        
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
                    self._removeTags(node, tags)
        except:
            return
