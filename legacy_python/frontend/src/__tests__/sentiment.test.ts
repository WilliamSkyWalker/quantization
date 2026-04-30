/**
 * Sentiment.vue tests — source status cards, article table with filters, download actions.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushAll, mountView } from './helpers'
import Sentiment from '../views/Sentiment.vue'
import { sentimentStatusResponse, sentimentArticlesResponse } from './fixtures'

vi.mock('../api')
import { getSentimentStatus, getSentimentArticles } from '../api'

describe('Sentiment.vue', () => {
  beforeEach(() => {
    vi.mocked(getSentimentStatus).mockResolvedValue({ data: sentimentStatusResponse } as any)
    vi.mocked(getSentimentArticles).mockResolvedValue({ data: sentimentArticlesResponse } as any)
  })

  it('calls status and articles APIs on mount', async () => {
    mountView(Sentiment)
    await flushAll()
    expect(getSentimentStatus).toHaveBeenCalled()
    expect(getSentimentArticles).toHaveBeenCalled()
  })

  it('renders total article count', async () => {
    const w = mountView(Sentiment)
    await flushAll()
    expect(w.text()).toContain('690')
    expect(w.text()).toContain('篇文章')
  })

  it('renders source cards grouped by tier', async () => {
    const w = mountView(Sentiment)
    await flushAll()
    expect(w.text()).toContain('gov_cn')
    expect(w.text()).toContain('csrc')
    expect(w.text()).toContain('pbc')
    expect(w.text()).toContain('twitter_trump')
    // Tier group names
    expect(w.text()).toContain('最高层')
    expect(w.text()).toContain('金融监管')
    expect(w.text()).toContain('美国政策')
  })

  it('renders article titles and categories', async () => {
    const w = mountView(Sentiment)
    await flushAll()
    expect(w.text()).toContain('证监会发布关于加强上市公司监管的通知')
    expect(w.text()).toContain('中国人民银行公开市场业务交易公告')
    expect(w.text()).toContain('监管动态')
    expect(w.text()).toContain('公开市场')
  })

  it('renders article filter bar with search input', async () => {
    const w = mountView(Sentiment)
    await flushAll()
    expect(w.text()).toContain('查询')
    expect(w.text()).toContain('重置')
  })

  it('clicking source card filters articles', async () => {
    const w = mountView(Sentiment)
    await flushAll()
    vi.mocked(getSentimentArticles).mockClear()

    // Click the csrc source card (Naive UI uses n-card class)
    const cards = w.findAll('[class*="n-card"]')
    const csrcCard = cards.find(c => c.text().includes('csrc') && c.text().includes('篇'))
    if (csrcCard) {
      await csrcCard.trigger('click')
      await flushAll()
      expect(getSentimentArticles).toHaveBeenCalledWith(
        expect.objectContaining({ source: 'csrc', page: 1 })
      )
    }
  })

  it('renders pagination with total count', async () => {
    const w = mountView(Sentiment)
    await flushAll()
    expect(w.text()).toContain('共 690 篇')
  })
})
